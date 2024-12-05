#!/usr/bin/env python3
#
# Copyright (C) 2024 Clyso GmbH
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
quick and dirty ceph perf counter histogram formatter
for op latency x bytes histograms
"""

import json
import sys

def format_duration(nanos):
    if nanos < 1000:
        return f"{nanos}ns"
    elif nanos < 1000000:
        return f"{int(nanos/1000)}µs"
    elif nanos < 1000000000:
        return f"{int(nanos/1000000)}ms"
    else:
        return f"{int(nanos/1000000000)}s"

def format_bytes(size):
    n = 0
    units = ['', 'K', 'M', 'G', 'T']
    while size >= 1024 and n < len(units) - 1:
        size /= 1024
        n += 1
    return f"{size:.0f}{units[n]}"

# TODO don't assume we have time x bytes axes
def perf_hist_dump_1d(data):
    axes = data["axes"]
    ax_time = axes[0]
    vals = [sum(x) for x in data["values"]]
    for i, range in enumerate(ax_time["ranges"]):
        if "max" in range and range["max"] == -1:
            line_header = "<0"
        elif "max" not in range:
            line_header = f">{format_duration(ax_time['ranges'][i-1]['max'])}"
        else:
            line_header = f"{format_duration(range["min"])}…{format_duration(range["max"])}"
        print(f"{line_header}\t{vals[i]}")

# TODO don't assume we have time x bytes axes
def perf_hist_dump_2d(data):
    axes = data["axes"]
    ax_time = axes[0]
    ax_bytes = axes[1]
    vals = data["values"]
    cutoff_bytes = 32*1024**2
    cutoff_time = 104*10**9
    headers = []
    for i, col_range in enumerate(ax_bytes["ranges"]):
        if "max" in col_range and col_range["max"] == -1:
            # < zero bytes would be very odd..
            pass
            #headers.append("<0")
        elif "max" not in col_range:
            headers.append(f">{format_bytes(ax_bytes['ranges'][i-1]['max'])}")
        elif col_range["max"] > cutoff_bytes:
            headers.append(f">{format_bytes(cutoff_bytes)}")
            break
        else:
            headers.append(f"{format_bytes(col_range["min"])}…{format_bytes(col_range["max"])}")
    print("⬔\t", "\t".join(headers))

    for row, row_range in enumerate(ax_time["ranges"]):
        line_header = ""
        if "max" in row_range and row_range["max"] == -1:
            line_header = "<0"
        elif "max" not in row_range:
            line_header = f">{format_duration(ax_time["ranges"][row-1]["max"])}"
        elif row_range["max"] > cutoff_time:
            line_header = f">{format_duration(cutoff_time)}"
        else:
            line_header = f"{format_duration(row_range["min"])}…{format_duration(row_range["max"])}"

        cols = []
        for i in range(1, len(vals[row])):
            if ax_bytes["ranges"][i].get("max", -1) <= cutoff_bytes:
                cols.append(str(vals[row][i]))
            else:
                cols.append(str(sum((vals[row][j] for j in range(i, len(vals[row]))))))
                break
        print(f"{line_header}\t{'\t'.join(cols)}")
        if row_range["max"] > cutoff_time:
            break

def main():
    if not sys.stdin.isatty():
        data = json.load(sys.stdin)
    elif len(sys.argv) > 2:
        with open(sys.argv[2]) as fp:
            data = json.load(fp)
    else:
        sys.exit(1)
    {"1d": perf_hist_dump_1d,
     "2d": perf_hist_dump_2d}[sys.argv[1]](data)

if __name__ == '__main__':
    main()
