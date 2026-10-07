"""Does the Python SDK drop an envelope when its pooled keep-alive connection was closed by the server?
A local fake ingest keeps connections alive but closes them after IDLE seconds without a request
(what load balancers do). Two spans are sent GAP seconds apart. Nothing leaves the machine.
usage: python stale_conn_repro.py GAP     (e.g. 3 = gap longer than the idle timeout, 0.3 = control)
"""
import socket, sys, threading, time
import sentry_sdk

IDLE, GAP = 1.0, float(sys.argv[1])
received = []

def handle(conn):
    conn.settimeout(IDLE)
    buf = b""
    try:
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                return  # idle too long: close the connection, as a load balancer would
            if not chunk:
                return
            buf += chunk
            while b"\r\n\r\n" in buf:
                head, rest = buf.split(b"\r\n\r\n", 1)
                n = int([l.split(b":")[1] for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")][0])
                while len(rest) < n:
                    rest += conn.recv(65536)
                received.append(time.monotonic())
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\n{}")
                buf = rest[n:]
    finally:
        conn.close()

srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen()
port = srv.getsockname()[1]
threading.Thread(target=lambda: [threading.Thread(target=handle, args=(srv.accept()[0],), daemon=True).start() for _ in iter(int, 1)], daemon=True).start()

lost = []
sentry_sdk.init(dsn=f"http://key@127.0.0.1:{port}/1", traces_sample_rate=1.0, trace_lifecycle="stream")
orig = sentry_sdk.get_client().transport.record_lost_event
sentry_sdk.get_client().transport.record_lost_event = lambda reason, *a, **k: (lost.append(reason), orig(reason, *a, **k))

for i in range(2):
    with sentry_sdk.traces.start_span(name=f"span {i}"):
        pass
    sentry_sdk.flush(2)
    if i == 0:
        time.sleep(GAP)
sentry_sdk.flush(2)
print(f"gap {GAP}s (server idle timeout {IDLE}s): sent 2 envelopes, server received {len(received)}, lost reasons: {lost}")
