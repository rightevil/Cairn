"""A real console for design sessions; development workers keep their existing transport."""
import os
import re
import signal
import subprocess
import codecs


class ConsoleProcess:
    def __init__(self, argv, cwd, env, rows=32, cols=110):
        self.job = None
        self.tail = ""
        self.ready = False
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        if os.name == "nt":
            from winpty import PtyProcess
            from cairn.dispatcher.runtime.windows_job import WindowsJob
            self.process = PtyProcess.spawn(argv, cwd=str(cwd), env={**os.environ, **env}, dimensions=(rows, cols))
            self.pid = self.process.pid
            try:
                self.job = WindowsJob()
                self.job.assign_and_resume(self.pid)
            except BaseException:
                self.process.close(force=True)
                if self.job: self.job.close()
                raise
        else:
            import pty
            self.master, slave = pty.openpty()
            try:
                self.process = subprocess.Popen(argv, cwd=cwd, env={**os.environ, **env}, stdin=slave,
                    stdout=slave, stderr=slave, start_new_session=True)
            finally:
                os.close(slave)
            self.pid = self.process.pid
            self.resize(rows, cols)

    def read(self):
        if os.name == "nt": return self.process.read(16384)
        return self.decoder.decode(os.read(self.master, 16384))

    def write(self, value):
        if os.name == "nt": self.process.write(value)
        else: os.write(self.master, value.encode("utf-8"))

    def resize(self, rows, cols):
        rows, cols = max(8, min(int(rows), 150)), max(30, min(int(cols), 400))
        if os.name == "nt": self.process.setwinsize(rows, cols)
        else:
            import fcntl, termios, struct
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def alive(self):
        return self.process.isalive() if os.name == "nt" else self.process.poll() is None

    def answer_queries(self, chunk):
        # One reply owner: a closed browser must not stall a background review.
        text = self.tail + chunk
        boundary = len(self.tail)
        pattern = r'\x1b\[(?:6n|c|0c|>c|>0c)|\x1b\](?:10|11);\?(?:\x07|\x1b\\)'
        for match in re.finditer(pattern, text):
            if match.end() <= boundary: continue
            query = match[0]
            if query.endswith('6n'): self.ready = True
            answer = ("\x1b[1;1R" if query.endswith("6n") else "\x1b[>0;0;0c" if ">" in query
                      else "\x1b[?1;2c" if query.endswith("c") else
                      "\x1b]" + ("10;rgb:2929/3030/3a3a" if "]10;" in query else "11;rgb:f7f7/f5f5/f0f0") + "\x1b\\")
            self.write(answer)
        self.tail = text[-40:]

    def close(self):
        if os.name == "nt":
            if self.job:
                self.job.close(); self.job = None
            self.process.close(force=True)
        else:
            if self.process.poll() is None:
                os.killpg(self.pid, signal.SIGTERM)
                try: self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.pid, signal.SIGKILL); self.process.wait(timeout=3)
            os.close(self.master)
