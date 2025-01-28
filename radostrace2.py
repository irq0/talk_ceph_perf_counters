#!/usr/bin/env python3

from bcc import BPF, USDT
import time
import sys
import ctypes

bpf_text = r"""
#include <uapi/linux/ptrace.h>

struct event_t {
  uint64_t tid;
  uint64_t start_ts;
  char object[256];
  char ops[1024];
};

BPF_RINGBUF_OUTPUT(events, 8);

int trace_send_op(struct pt_regs *ctx)
{

    struct event_t* ev = events.ringbuf_reserve(sizeof(struct event_t));
    if (!ev) {
        return 1;
    }
    bpf_usdt_readarg(1, ctx, &ev->tid);
    ev->start_ts = bpf_ktime_get_ns();

    uint64_t addr_obj;
    bpf_usdt_readarg(3, ctx, &addr_obj);
    bpf_probe_read_user_str(&ev->object, sizeof(ev->object), (void *)addr_obj);

    uint64_t addr_ops;
    bpf_usdt_readarg(5, ctx, &addr_ops);
    bpf_probe_read_user_str(&ev->ops, sizeof(ev->ops), (void *)addr_ops);

    events.ringbuf_submit(ev, 0);
    return 0;
}


"""

# Hold in sync with event_data_t above
class ObjecterEvent(ctypes.Structure):
    _fields_ = [("tid", ctypes.c_ulonglong),
                ("start_ts", ctypes.c_ulonglong),
                ("raw_object", ctypes.c_char * 256),
                ("raw_ops", ctypes.c_char * 1024)]
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
    b = BPF(text=bpf_text, usdt_contexts=[u])

    def callback(_ctx, data, _size):
        event = ctypes.cast(data, ctypes.POINTER(ObjecterEvent)).contents
        print(f"[{event.tid:>10}]\t{event.object():>50}\t"
              f"{' → '.join(event.ops())}\t\t")

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
