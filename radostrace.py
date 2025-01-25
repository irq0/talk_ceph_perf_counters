#!/usr/bin/env python3

from bcc import BPF, USDT
import time
import sys
import ctypes

bpf_text = r"""
#include <uapi/linux/ptrace.h>

struct in_flight_data_t {
  uint64_t tid;
  uint64_t start_ts;
  char object[100];
  char ops[350];
};

struct event_data_t {
  uint64_t tid;
  uint64_t duration;
  char object[100];
  char ops[350];
};

BPF_HASH(in_flight_hash, u64, struct in_flight_data_t);
BPF_RINGBUF_OUTPUT(events, 8);

int trace_send_op(struct pt_regs *ctx)
{
    struct in_flight_data_t data = {};

    bpf_usdt_readarg(1, ctx, &data.tid);
    data.start_ts = bpf_ktime_get_ns();

    uint64_t addr_obj;
    bpf_usdt_readarg(3, ctx, &addr_obj);
    bpf_probe_read_user_str(&data.object, sizeof(data.object), (void *)addr_obj);

    uint64_t addr_ops;
    bpf_usdt_readarg(5, ctx, &addr_ops);
    bpf_probe_read_user_str(&data.ops, sizeof(data.ops), (void *)addr_ops);

    in_flight_hash.update(&data.tid, &data);
    return 0;
}

int trace_finish_op(struct pt_regs *ctx)
{
    struct event_data_t ev = {};
    bpf_usdt_readarg(1, ctx, &ev.tid);

    struct in_flight_data_t* in_flight = in_flight_hash.lookup(&ev.tid);
    if (in_flight == NULL) {
       return 0;
    }

    ev.duration = bpf_ktime_get_ns() - in_flight->start_ts;
    __builtin_memcpy(&ev.object, &in_flight->object, 100);
    __builtin_memcpy(&ev.ops, &in_flight->ops, 350);
    events.ringbuf_output(&ev, sizeof(ev), 0);
    in_flight_hash.delete(&ev.tid);
}
"""

# Hold in sync with event_data_t above
class ObjecterEvent(ctypes.Structure):
    _fields_ = [("tid", ctypes.c_ulonglong),
                ("duration_ns", ctypes.c_ulonglong),
                ("raw_object", ctypes.c_char * 100),
                ("raw_ops", ctypes.c_char * 350)]
    def object(self):
        return self.raw_object.decode('utf-8')
    def ops(self):
        return self.raw_ops.decode('utf-8').split(";")


def trace_rados_ops():
    if (len(sys.argv) != 2):
        print("USAGE: radostrace.py <rados client PID>")
        sys.exit(1)

    u = USDT(pid=int(sys.argv[1]))
    u.enable_probe(probe="objecter_send_op", fn_name="trace_send_op")
    u.enable_probe(probe="objecter_finish_op", fn_name="trace_finish_op")
    b = BPF(text=bpf_text, usdt_contexts=[u])

    def callback(_ctx, data, _size):
        event = ctypes.cast(data, ctypes.POINTER(ObjecterEvent)).contents
        print(f"[{event.tid:>10}]\t{event.object():>50}\t"
              f"{' → '.join(event.ops()):<50s}\t\t"
              f"{event.duration_ns/1000000:>10.2f}ms")

    b["events"].open_ring_buffer(callback)

    print("tracing rados ops. hit ctrl-c to exit.")
    try:
        while 1:
            b.ring_buffer_poll()
            time.sleep(0.5)
    except KeyboardInterrupt:
        sys.exit()

if __name__ == '__main__':
    trace_rados_ops()
