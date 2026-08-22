"""
Stress and performance benchmark for GET /api/config and PUT /api/config (#88913).

Exercises concurrent reads and writes, CAS revision conflict rates, and latency.
"""

import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import time
import tempfile
import threading
from starlette.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.config import save_config


def run_stress_test():
    with tempfile.TemporaryDirectory() as tmp_dir:
        import os
        os.environ["HERMES_HOME"] = tmp_dir

        web_server.app.state.bound_host = "127.0.0.1"
        web_server.app.state.bound_port = 9119
        web_server.app.state.auth_required = False

        initial_config = {
            "agent": {"personality": "default"},
            "model": {"default": "azure/gpt-5.6-sol", "provider": "bifrost"},
            "dashboard": {"basic_auth": {"username": "admin", "secret": "sec123"}},
        }
        save_config(initial_config)

        headers = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}
        client = TestClient(web_server.app, base_url="http://127.0.0.1:9119", headers=headers)

        print("\n--- Starting Hermes Config Endpoint Stress Test ---")

        # 1. Stress Test: Concurrent Reads (GET /api/config)
        read_iterations = 200
        num_threads = 10
        read_times = []
        errors = []

        def worker_read():
            for _ in range(read_iterations // num_threads):
                t0 = time.perf_counter()
                try:
                    res = client.get("/api/config")
                    dt = (time.perf_counter() - t0) * 1000.0
                    read_times.append(dt)
                    if res.status_code != 200 or "_revision" not in res.json():
                        errors.append(f"GET failed: status {res.status_code}")
                except Exception as e:
                    errors.append(str(e))

        threads = [threading.Thread(target=worker_read) for _ in range(num_threads)]
        t_start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        total_read_time = time.perf_counter() - t_start

        avg_read = sum(read_times) / len(read_times) if read_times else 0
        read_times.sort()
        p95_read = read_times[int(len(read_times) * 0.95)] if read_times else 0
        p99_read = read_times[int(len(read_times) * 0.99)] if read_times else 0
        rps_read = len(read_times) / total_read_time if total_read_time > 0 else 0

        print(f"[READ STRESS] Total Requests: {len(read_times)} in {total_read_time:.3f}s ({rps_read:.1f} req/s)")
        print(f"  Avg Latency: {avg_read:.2f} ms | P95: {p95_read:.2f} ms | P99: {p99_read:.2f} ms")
        print(f"  Read Errors: {len(errors)}")

        # 2. Stress Test: Concurrent Writes with Revision Token (CAS)
        write_iterations = 100
        conflicts = []
        successful_writes = []
        write_times = []

        def worker_write(worker_id):
            for i in range(write_iterations // 5):
                t0 = time.perf_counter()
                try:
                    # GET latest revision
                    res_get = client.get("/api/config")
                    rev = res_get.json().get("_revision")

                    # Attempt CAS PUT
                    res_put = client.put(
                        "/api/config",
                        json={
                            "config": {"agent": {"personality": f"personality_w{worker_id}_{i}"}},
                            "expected_revision": rev,
                        },
                    )
                    dt = (time.perf_counter() - t0) * 1000.0
                    write_times.append(dt)

                    if res_put.status_code == 200:
                        successful_writes.append(res_put.json().get("_revision"))
                    elif res_put.status_code == 409:
                        conflicts.append("409_conflict")
                    else:
                        errors.append(f"PUT unexpected status: {res_put.status_code}")
                except Exception as e:
                    errors.append(str(e))

        write_threads = [threading.Thread(target=worker_write, args=(w,)) for w in range(5)]
        t_start_w = time.perf_counter()
        for t in write_threads:
            t.start()
        for t in write_threads:
            t.join()
        total_write_time = time.perf_counter() - t_start_w

        avg_write = sum(write_times) / len(write_times) if write_times else 0
        write_times.sort()
        p95_write = write_times[int(len(write_times) * 0.95)] if write_times else 0
        rps_write = len(write_times) / total_write_time if total_write_time > 0 else 0

        print(f"\n[WRITE CAS STRESS] Total Attempts: {len(write_times)} in {total_write_time:.3f}s ({rps_write:.1f} req/s)")
        print(f"  Successful Writes: {len(successful_writes)} | Revision Conflicts (409): {len(conflicts)}")
        print(f"  Avg Latency: {avg_write:.2f} ms | P95: {p95_write:.2f} ms")
        print(f"  Write Errors: {len(errors)}")

        print("\n--- Stress Test Completed Successfully ---\n")
        assert len(errors) == 0, f"Stress test encountered errors: {errors}"


if __name__ == "__main__":
    run_stress_test()
