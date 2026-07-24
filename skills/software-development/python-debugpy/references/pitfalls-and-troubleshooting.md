# Common Pitfalls (pdb / debugpy)

1. **pdb under pytest-xdist silently does nothing.** You won't see the prompt, the test just hangs.
   Always use `-p no:xdist` or `-n 0`.

2. **`breakpoint()` in CI / non-TTY contexts hangs the process.** Safe locally; never commit it.
   Add a pre-commit grep as a safety net.

3. **`PYTHONBREAKPOINT=0`** disables all `breakpoint()` calls. Check the env if your breakpoint
   isn't hitting:
   ```bash
   echo $PYTHONBREAKPOINT
   ```

4. **`debugpy.listen` blocks only if you also call `wait_for_client()`.** Without it, execution
   continues and your first breakpoint may fire before the client is attached.

5. **Attach to PID fails on hardened kernels.** `ptrace_scope=1` (Ubuntu default) allows only
   same-user ptrace of child processes. Workaround: `echo 0 > /proc/sys/kernel/yama/ptrace_scope`
   (needs root) or launch under `debugpy` from the start.

6. **Threads.** `pdb` only debugs the current thread. For multithreaded code, use `debugpy`
   (thread-aware DAP) or set `threading.settrace()` per thread.

7. **asyncio.** `pdb` works in coroutines but `await` inside pdb requires Python 3.13+ or `await`
   from `interact` mode on older versions. For 3.11/3.12, use `asyncio.run_coroutine_threadsafe`
   tricks or `!stmt`-based awaits via `asyncio.ensure_future`.

8. **`scripts/run_tests.sh` strips credentials and sets `HOME=<tmpdir>`.** If your bug depends on
   user config or real API keys, it won't reproduce under the wrapper. Debug with raw `pytest` first
   to repro, then re-confirm under the wrapper.

9. **Forking / multiprocessing.** pdb does not follow forks. Each child needs its own
   `breakpoint()` or `set_trace()`. For Hermes subagents, debug one process at a time.
