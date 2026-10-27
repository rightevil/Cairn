"""Owned Claude subprocess transport shared by interactive design and frozen review."""
import os
from cairn.dispatcher.runtime.process_identity import process_identity


def owned_transport(dispatcher, pid, run, directory, options, session):
    # shortcut: SDK process ownership uses its internal transport; review on SDK upgrades.
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    class OwnedTransport(SubprocessCLITransport):
        job = None

        async def connect(self):
            try:
                await super().connect()
                process = self._process
                if os.name == "nt":
                    from cairn.dispatcher.runtime.windows_job import WindowsJob
                    self.job = WindowsJob()
                    self.job.assign_and_resume(process.pid)
                dispatcher.call(pid, f"runs/{run['id']}/heartbeat", {"token": run["token"], "pid": process.pid,
                    "process_identity": process_identity(process.pid), "session": session,
                    "streams": {"stdout": str(directory / "stdout.log"), "stderr": str(directory / "stderr.log")}})
            except BaseException:
                await self.close()
                raise

        async def close(self):
            try:
                await super().close()
            finally:
                if self.job:
                    self.job.close()
                    self.job = None

    return OwnedTransport(prompt=None, options=options)
