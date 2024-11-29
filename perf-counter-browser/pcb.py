#!/usr/bin/env python3
"""
first attempts at building a web-based performance counter
browser
"""

import rados
import logging
import os
import sys
import subprocess
import json
import itertools
from typing import Any, Callable, Dict, List, Optional, Tuple
from ceph_argparse import json_command
from pprint import pprint, pformat
import dominate
import dominate.tags as T
import pathlib
from flask import Flask, request, jsonify


SCRIPT_PATH = pathlib.Path(__file__).parent
LOG = logging.getLogger("cntop")
CephTarget = Tuple[str, Optional[str]]

def setup():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="[%Y-%m-%d %H:%M:%S]",
    )


def connect() -> rados.Rados:
    conffile = os.getenv("CEPH_CONF") or "/etc/ceph/ceph.conf"
    cluster = rados.Rados(conffile=conffile)
    cluster.connect()
    LOG.info("Connected to cluster %s", cluster.get_fsid())
    return cluster


def get_inventory(cluster: rados.Rados) -> Dict[str, List[CephTarget]]:
    """
    Get Ceph cluster inventory as dict of type -> [target, ...]
    """
    return {
        "osd": [
            ("osd", line.decode("utf-8"))
            for line in json_command(cluster, prefix="osd ls")[1].split(b"\n")
            if line
        ],
        "mon": [
            ("mon", m["name"])
            for m in json.loads(
                json_command(cluster, prefix="mon dump", argdict={"format": "json"})[
                    1
                ].decode("utf-8")
            )["mons"]
        ],
        "mgr": [
            (
                "mgr",
                json.loads(
                    json_command(
                        cluster, prefix="mgr dump", argdict={"format": "json"}
                    )[1].decode("utf-8")
                )["active_name"],
            )
        ],
        "mds": [
            ("mds", fs_info["name"])
            for fs_info in
            itertools.chain.from_iterable(
            (fs["mdsmap"]["info"].values()
             for fs in json.loads(
                     json_command(
                     cluster, prefix="fs dump", argdict={"format": "json"}
                 )[1].decode("utf-8"))["filesystems"]
             ))
        ],
    }


def mdsids():
    ret, outbuf, outs = json_command(cluster_handle, prefix='fs dump',
                                     argdict={'format': 'json'})
    if ret:
        raise RuntimeError('Can\'t contact mon for mds list')
    d = json.loads(outbuf.decode('utf-8'))
    l = []
    for info in d['standbys']:
        l.append(info['name'])
    for fs in d['filesystems']:
        for info in fs['mdsmap']['info'].values():
            l.append(info['name'])
    return l

def perf_schema(cluster: rados.Rados, target: CephTarget) -> Any:
    ret, outbuf, outs = json_command(
        cluster, target=target, prefix=f"counter schema"
    )
    if ret in (0, 1):
        return json.loads(outbuf)
    else:
        LOG.warning("Failed to get counter schema on %s (%d): %s", target, ret, outs)
        return {}

def perf_schema_local(cluster: rados.Rados, filename: str) -> Any:
    out = subprocess.check_output(["ceph", "daemon", filename, "counter", "schema"])
    return json.loads(out)


def perf_value(cluster: rados.Rados, target: CephTarget, logger, name) -> Any:
    ret, outbuf, outs = json_command(
        cluster, target=target, prefix=f"counter dump"
    )
    if ret in (0, 1):
        return [x + {"value": x["counters"][name]} for x in json.loads(outbuf)[logger]]
    else:
        LOG.warning("Failed to get counter schema on %s (%d): %s", target, ret, outs)
        return []


def make_id(type, target, logger, group, name, schema):
    return f"{group}_{name}"


def logger_to_group(logger: str):
    """
    heuristic to transform a logger name like
    rocksdb
    throttle-$throttle_name
    AsyncMessenger::$messenger_name
    into a "group" of like counters
    """
    if logger.startswith("AsyncMessenger::"):
        return "async messenger"
    if logger.startswith("bluestore-pricache"):
        return "bluestore pricache"
    if logger.startswith("finisher-"):
        return "finisher"
    if logger.startswith("mclock-shard-queue"):
        return "mclock shard queue"
    if logger.startswith("osd_scrub_dp_"):
        return "osd scrub dp"
    if logger.startswith("throttle-"):
        return "throttle"
    if logger.startswith("bluestore_throttle_"):
        return "throttle"
    if logger.startswith("finisher-"):
        return "finisher"
    if logger.startswith("prioritycache:"):
        return "prioritycache"
    if logger.startswith("objecter-0x"):
        return "objecter"
    if logger.startswith("osd_scrub_sh_"):
        return "osd scrub sh"
    return logger

def discover_all_schemas(cluster, inventory):
    # id -> { schema: {} | targets: [] | tags: set() |
    # target -> collection -> counter / histogram -> schema

    result = {}

    for type, targets in inventory.items():
        for target in targets:
            if isinstance(target, str):
                all_target_schemas = perf_schema_local(cluster, target)
            else:
                all_target_schemas = perf_schema(cluster, target)

            # each group has a list of labels, counter pairs for each label value
            for logger, labeled_counters in all_target_schemas.items():
                assert len(labeled_counters) >= 1

                group = logger_to_group(logger)
                labels = [lc["labels"]  for lc in labeled_counters]
                counters = labeled_counters[0]["counters"]
                assert all(counters == lc["counters"] for lc in labeled_counters), "counter schemas differ?!"

                for name, schema in counters.items():
                    id = make_id(type, target, logger, group, name, schema)
                    # separate out the prio as it changes between service types
                    prio = schema["priority"]
                    del schema["priority"]

                    target_entry = {
                        "target": target,
                        "priority": prio,
                        "logger": logger,
                    }

                    if id in result:
                        assert result[id]["schema"] == schema, \
                            f"mismatching counter schemas detected. unsupported: {result[id]['schema']} vs {schema}"
                        result[id]["targets"].append(target_entry)
                        result[id]["tags"].add(type)
                        result[id]["logger"].add(logger)
                    else:
                        result[id] = {
                            "logger": set((logger,)),
                            "group": group,
                            "name": name,
                            "schema": schema,
                            "labels": labels,
                            "targets": [target_entry],
                            "tags": set((type,)),
                        }

    return result

def serve_http(cluster, perf_counters):
    app = Flask(__name__)

    @app.route("/latest/<id>/<target>/<logger>/<name>", methods=["GET"])
    def latest(id, target, logger, name):
        c = perf_counters[id]
        target = ".".split(target)
        div = T.div()
        with div:
            T.pre(perf_value(cluster, target, logger, name))
        return div.render().encode("utf-8")

    @app.route("/details/<id>", methods=["GET"])
    def details(id):
        div = T.div()
        c = perf_counters[id]
        s = c["schema"]
        with div:
            with T.ul():
                T.li(f"Group: {c['group']}")
                T.li(f"Name: {c['name']}")
                with T.li("Logger:"):
                    with T.ul():
                        for l in c['logger']:
                            T.li(l)
                T.li(f"Description: {s['description']}")
                T.li(f"Tags: {' '.join(c['tags'])}")
                T.li(f"Unit: {s['units']}")
                T.li(f"Type: {s['value_type']}")
                T.li(f"Metric Type: {s['metric_type']}")
                with T.li("Exported at: [prio]"):
                    with T.ul():
                        for t in c["targets"]:
                            T.li(f"{'.'.join(t['target'])} [{t['priority']}] {t['logger']}")
        return div.render().encode("utf-8")

    @app.route("/", methods=["GET"])
    def index():
        doc = dominate.document(title="Perf Counter Browser", lang="en")
        with doc.head:
            T.meta(http_equiv="Content-Type", content="text/html; charset=utf-8")
            with open(SCRIPT_PATH / "pure-min.css") as fd:
                T.style(fd.read())
            T.meta(name="viewport", content="width=device-width, initial-scale=1")
            T.style(
            """
            html {
                scroll-padding-top: 4em;
            }
            body {
                font-family: Noto Sans, sans-serif;
                line-height: 1.7em;
                font-size: 13px;
            }
            .pcb-menu {
                text-align: center;
                background: #2d3e50;
                padding: 0.5em;
                color: white;
                font-size: 120%;
                font-weight: 400;
            }
            .pcb-menu ul {
                float: right;
            }
            .content-wrapper {
                padding: 1em 1em 3em;
                margin-top: 4em;
            }
            .pcb-menu a {
                color: white;
            }
            .pcb-menu li a:hover,
            .pcb-menu li a:focus {
                background: none;
                border: none;
                color: white;
            }
            .details {
               color: #555;
               display: none;
            }
            .details-show {
               display: table-row;
            }
            """
            )
        with doc:
            with T.div(klass="header"), T.div(
                    klass="pure-menu pcb-menu pure-menu-horizontal pure-menu-fixed"
            ):
                T.a("Perf Counter Browser", href="#", klass="pure-menu-heading pcb-menu-brand")
                # with T.ul(klass="pure-menu-list"):
                    # T.li(
                    #     T.input_(type="text", id="search", klass="search-input", placeholder="TODO"),
                    #     klass="pure-menu-item",
                    # )

            with T.div(klass="content-wrapper") as div:
                div.add(T.p(f"Found {len(perf_counters)} perf counters in "
                            f"{len(set((c['group'] for c in perf_counters.values())))} groups"))
                table = div.add(T.table(id="perf-counter-table", klass="pure-table pure-table-striped"))
                thead = table.add(T.thead())
                with thead.add(T.tr()):
                    T.th("Group")
                    T.th("Name")
                    T.th("Description")
                    T.th("Type")
                    T.th("Tags")
                tbody = table.add(T.tbody())
                for id, c in perf_counters.items():
                    with tbody.add(T.tr(id=id, data_id=id)):
                        T.td(c["group"])
                        T.td(c["name"])
                        T.td(c["schema"]["description"])
                        T.td(c["schema"]["value_type"])
                        T.td(", ".join(c["tags"]))

            T.script(dominate.util.raw(
                """
                const table = document.getElementById('perf-counter-table');
                const rows = Array.from(table.getElementsByTagName('tr'));
                rows.forEach((row, index) => {
                   if (index >= 0) {
                      row.addEventListener("click", function() {
                         const id = row.getAttribute("data-id");
                         const details = document.getElementById(`details_${id}`);
                         console.log(details)
                         if (details) {
                            details.classList.toggle("details-show");
                         } else {
                            const new_row = document.createElement("tr");
                            new_row.id = `details_${id}`;
                            const td = document.createElement("td");
                            td.classList.add("metadata");
                            td.setAttribute("colspan", "2");
                            const tdvals = document.createElement("td");
                            td.classList.add("values");
                            tdvals.setAttribute("colspan", "2");
                            new_row.appendChild(td);
                            new_row.appendChild(tdvals);
                            new_row.classList.add("details", "details-show");
                            row.insertAdjacentElement("afterend", new_row);
                            fetch_details(id, td);
                        }
                      });
                   }
                });

                function fetch_details(id, div) {
                   fetch(`/details/${id}`).then(
                      response => {
                         if(!response.ok) {
                              throw new Error(":(");
                         }
                         return response.text();
                    })
                    .then(data => {
                         console.log(data);
                         div.innerHTML = data;
                    })
                    .catch(error => {
                         div.innerHTML = `Error ${error.message}`;
                    })
                }
                function fetch_values(id, target, logger, name, dest) {
                   fetch(`/value/${id}/${target}/${logger}/${name}`).then(
                      response => {
                         if(!response.ok) {
                              throw new Error(":(");
                         }
                         return response.text();
                    })
                    .then(data => {
                         console.log(data);
                         dest.innerHTML = data;
                    })
                    .catch(error => {
                         dest.innerHTML = `Error ${error.message}`;
                    })
                }
                """))



        return doc.render().encode("utf-8")
    app.run(debug=True, host="::", port="9999")

def main():
    setup()
    cluster = connect()
    inv = get_inventory(cluster)
    if len(sys.argv) > 1:
        inv["client"] = sys.argv[1:]
    pprint(inv)
    schemas = discover_all_schemas(cluster, inv)
    serve_http(cluster, schemas)

if __name__ == "__main__":
    main()
