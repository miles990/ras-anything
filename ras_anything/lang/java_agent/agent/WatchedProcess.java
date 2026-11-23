package _ras_agent;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.io.OutputStream;
import java.nio.file.Files;
import java.util.Arrays;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;

/** A subprocess started through the agent: real (scan, chaos, record, bench) or replayed on the virtual clock (verify). */
final class WatchedProcess {
    private WatchedProcess() {}

    static Process start(ProcessBuilder pb) throws IOException {
        Agent.Req r = Agent.Req.process(pb.command());
        if (Agent.verify) {
            Agent.Fate f = Agent.replay(r);
            if (f.out != null && f.out.exc instanceof IOException e) throw e;
            return new Replayed(pb, f);
        }
        Agent.Attempt a = Agent.liveAttempt(r);
        String fault = a.fault == null ? "" : a.fault;
        if (fault.equals("refused")) throw (IOException) Agent.refusedError(r);
        if (fault.equals("timeout") || fault.startsWith("http")) return new Dead(r, a, fault.equals("timeout"));
        long t0 = System.nanoTime();
        a.reached = true;
        Process real;
        try {
            real = pb.start();
        } catch (IOException e) {
            Agent.recordIo(r, Agent.Out.of(e), t0);
            throw e;
        }
        a.failed = false;
        return new Running(pb, real, r, a, t0);
    }

    private abstract static class Base extends Process {
        @Override
        public long pid() {
            return 4242;
        }

        @Override
        public Stream<ProcessHandle> children() {
            return Stream.empty();
        }

        @Override
        public Stream<ProcessHandle> descendants() {
            return Stream.empty();
        }

        @Override
        public boolean supportsNormalTermination() {
            return true;
        }
    }

    private static InterruptedIOException interrupted() {
        Thread.currentThread().interrupt();
        return new InterruptedIOException();
    }

    /** A stream whose bytes are known once `ready` returned. */
    private abstract static class Late extends InputStream {
        private byte[] data;
        private int pos;

        abstract byte[] ready() throws IOException;

        private byte[] data() throws IOException {
            return data == null ? data = ready() : data;
        }

        @Override
        public int read() throws IOException {
            return pos < data().length ? data[pos++] & 255 : -1;
        }

        @Override
        public int read(byte[] b, int off, int len) throws IOException {
            if (len == 0) return 0;
            int n = Math.min(len, data().length - pos);
            if (n <= 0) return -1;
            System.arraycopy(data, pos, b, off, n);
            pos += n;
            return n;
        }

        @Override
        public int available() {
            return data == null ? 0 : data.length - pos;
        }
    }

    // ---------------------------------------------------------------- verify

    private static final class Replayed extends Base {
        private final ProcessBuilder pb;
        private final Agent.Fate f;
        private final long endAt;
        private final CountDownLatch killed = new CountDownLatch(1);
        private final CompletableFuture<Process> exit = new CompletableFuture<>();
        private volatile int code;
        private boolean wrote;

        Replayed(ProcessBuilder pb, Agent.Fate f) {
            this.pb = pb;
            this.f = f;
            endAt = f.hang ? Long.MAX_VALUE : Agent.clock.now() + Agent.nanos(f.delay);
            code = f.hang ? 0 : f.out.status;
            if (!f.hang) Agent.clock.schedule(endAt, () -> exit.complete(this));
        }

        private boolean ended() {
            return killed.getCount() == 0 || Agent.clock.now() >= endAt;
        }

        private byte[] out() {
            return f.hang || (killed.getCount() == 0) ? new byte[0] : f.out.body;
        }

        private synchronized int finish() {
            exit.complete(this);
            if (wrote || f.hang || killed.getCount() == 0) return code;
            wrote = true;
            try {
                spill(pb.redirectOutput(), f.out.body, System.out);
                if (!pb.redirectErrorStream()) spill(pb.redirectError(), f.out.err, System.err);
            } catch (IOException e) {
                throw new java.io.UncheckedIOException(e);
            }
            return code;
        }

        private static void spill(ProcessBuilder.Redirect to, byte[] data, OutputStream std) throws IOException {
            if (to == ProcessBuilder.Redirect.INHERIT) std.write(data);
            else if (to.file() != null && to != ProcessBuilder.Redirect.DISCARD) {
                if (to.type() == ProcessBuilder.Redirect.Type.APPEND)
                    Files.write(to.file().toPath(), data, java.nio.file.StandardOpenOption.CREATE, java.nio.file.StandardOpenOption.APPEND);
                else Files.write(to.file().toPath(), data);
            }
        }

        /** Until it ends or is killed. */
        private void await() throws InterruptedException {
            if (ended()) return;
            if (Agent.onTrial()) {
                if (f.hang) Agent.vhang(f.req.key);
                while (!ended()) Agent.vwait(Math.max(1, endAt - Agent.clock.now()));
                return;
            }
            Agent.Trial t = Agent.trial;
            while (!exit.isDone()) { // until its end comes up on the clock, or someone kills it
                if (t == null || t.over) throw new ChaosHang("the call is over");
                Thread.sleep(1);
            }
        }

        @Override
        public int waitFor() throws InterruptedException {
            await();
            return finish();
        }

        @Override
        public boolean waitFor(long n, TimeUnit u) throws InterruptedException {
            long ns = u.toNanos(n), left = endAt == Long.MAX_VALUE ? Long.MAX_VALUE : endAt - Agent.clock.now();
            if (!ended()) Agent.vwait(Math.min(ns, left));
            if (!ended()) {
                f.rec.failed = true;
                return false;
            }
            finish();
            return true;
        }

        @Override
        public int exitValue() {
            if (!ended()) throw new IllegalThreadStateException("process hasn't exited");
            return finish();
        }

        @Override
        public boolean isAlive() {
            if (ended()) finish();
            return !ended();
        }

        private void kill(int c) {
            if (ended()) return;
            code = c;
            f.rec.failed = true;
            killed.countDown();
            exit.complete(this);
        }

        @Override
        public void destroy() {
            kill(143);
        }

        @Override
        public Process destroyForcibly() {
            kill(137);
            return this;
        }

        @Override
        public CompletableFuture<Process> onExit() {
            return exit.thenApply(p -> p);
        }

        private InputStream stream(ProcessBuilder.Redirect to, boolean stdout) {
            if (to != ProcessBuilder.Redirect.PIPE || (!stdout && pb.redirectErrorStream())) return InputStream.nullInputStream();
            return new Late() {
                @Override
                byte[] ready() throws IOException {
                    try {
                        await();
                    } catch (InterruptedException e) {
                        throw interrupted();
                    }
                    return stdout ? out() : killed.getCount() == 0 || f.hang ? new byte[0] : f.out.err;
                }
            };
        }

        private final InputStream[] streams = new InputStream[2];

        @Override
        public synchronized InputStream getInputStream() {
            return streams[0] == null ? streams[0] = stream(pb.redirectOutput(), true) : streams[0];
        }

        @Override
        public synchronized InputStream getErrorStream() {
            return streams[1] == null ? streams[1] = stream(pb.redirectError(), false) : streams[1];
        }

        @Override
        public OutputStream getOutputStream() {
            return OutputStream.nullOutputStream();
        }
    }

    // ---------------------------------------------------------------- live

    /** An injected failure instead of a process: it never ends, or exits 75 at once. */
    private static final class Dead extends Base {
        private final Agent.Req r;
        private final boolean hangs;
        private final CountDownLatch killed = new CountDownLatch(1);
        private final CompletableFuture<Process> exit = new CompletableFuture<>();
        private volatile int code = 75;

        Dead(Agent.Req r, Agent.Attempt a, boolean hangs) {
            this.r = r;
            this.hangs = hangs;
            a.reached = false;
            if (!hangs) {
                killed.countDown();
                exit.complete(this);
            }
        }

        private void await() throws InterruptedException {
            long end = System.nanoTime() + Agent.nanos(Agent.hangS);
            if (isAlive()) synchronized (Agent.LOCK) {
                Agent.hanging++;
            }
            else return;
            try {
                while (!killed.await(5, TimeUnit.MILLISECONDS)) {
                    if (System.nanoTime() < end) continue;
                    if (!bounded) Agent.declareHang(r.key);
                }
            } finally {
                synchronized (Agent.LOCK) {
                    Agent.hanging--;
                }
            }
        }

        private volatile boolean bounded;

        @Override
        public int waitFor() throws InterruptedException {
            await();
            return code;
        }

        @Override
        public boolean waitFor(long n, TimeUnit u) throws InterruptedException {
            bounded = true;
            return killed.await(Math.round(Math.min(u.toNanos(n) / 1e9 * Agent.scale, Agent.hangS) * 1e9), TimeUnit.NANOSECONDS);
        }

        @Override
        public int exitValue() {
            if (isAlive()) throw new IllegalThreadStateException("process hasn't exited");
            return code;
        }

        @Override
        public boolean isAlive() {
            return killed.getCount() > 0;
        }

        private void kill(int c) {
            if (!isAlive()) return;
            code = c;
            killed.countDown();
            exit.complete(this);
        }

        @Override
        public void destroy() {
            kill(143);
        }

        @Override
        public Process destroyForcibly() {
            kill(137);
            return this;
        }

        @Override
        public CompletableFuture<Process> onExit() {
            bounded |= hangs; // a future: whoever waits for it has its own timeout, or the run's
            return exit.thenApply(p -> p);
        }

        private final InputStream in = late(), err = late();

        private InputStream late() {
            return new Late() {
                @Override
                byte[] ready() throws IOException {
                    try {
                        await();
                    } catch (InterruptedException e) {
                        throw interrupted();
                    }
                    return new byte[0];
                }
            };
        }

        @Override
        public InputStream getInputStream() {
            return in;
        }

        @Override
        public InputStream getErrorStream() {
            return err;
        }

        @Override
        public OutputStream getOutputStream() {
            return OutputStream.nullOutputStream();
        }
    }

    /** Reads a stream to its end in the background; readers get the bytes as they come (or cut in half at the end). */
    private static final class Drain extends InputStream {
        private final ByteArrayOutputStream buf = new ByteArrayOutputStream();
        private final boolean cut;
        private boolean eof;
        private int pos;

        Drain(InputStream from, boolean cut) {
            this.cut = cut;
            Thread t = new Thread(() -> {
                byte[] b = new byte[8192];
                try (from) {
                    for (int n; (n = from.read(b)) >= 0; ) synchronized (this) {
                        buf.write(b, 0, n);
                        notifyAll();
                    }
                } catch (IOException e) {
                    // the process is gone
                }
                synchronized (this) {
                    eof = true;
                    notifyAll();
                }
            }, "ras-drain");
            t.setDaemon(true);
            t.start();
        }

        synchronized byte[] all() throws InterruptedException {
            while (!eof) wait();
            return buf.toByteArray();
        }

        @Override
        public int read() throws IOException {
            byte[] one = new byte[1];
            return read(one, 0, 1) < 0 ? -1 : one[0] & 255;
        }

        @Override
        public synchronized int read(byte[] b, int off, int len) throws IOException {
            if (len == 0) return 0;
            try {
                while (!eof && (cut || pos >= buf.size())) wait();
            } catch (InterruptedException e) {
                throw interrupted();
            }
            byte[] have = buf.toByteArray();
            int n = Math.min(len, (cut ? have.length / 2 : have.length) - pos);
            if (n <= 0) return -1;
            System.arraycopy(have, pos, b, off, n);
            pos += n;
            return n;
        }

        @Override
        public synchronized int available() {
            return cut && !eof ? 0 : Math.max(0, (cut ? buf.size() / 2 : buf.size()) - pos);
        }
    }

    private static final class Running extends Base {
        private final ProcessBuilder pb;
        private final Process real;
        private final Agent.Req r;
        private final Agent.Attempt a;
        private final long t0;
        private final Drain in, err;
        private final Map<String, Object> entry;
        private double extra;
        private boolean closed;

        Running(ProcessBuilder pb, Process real, Agent.Req r, Agent.Attempt a, long t0) {
            this.pb = pb;
            this.real = real;
            this.r = r;
            this.a = a;
            this.t0 = t0;
            extra = "slow".equals(a.fault) ? Agent.slowS : 0;
            in = new Drain(real.getInputStream(), "truncated".equals(a.fault));
            err = new Drain(real.getErrorStream(), false);
            Agent.Scope s = Agent.mode.equals("record") ? Agent.current() : null;
            entry = s == null ? null : Json.map("req", r.json(), "out", Agent.Out.of(-1, new byte[0], null).json(r), "dur", 0.0);
            if (s != null) s.io.add(entry);
        }

        /** The injected extra seconds, if the caller waits that long. */
        private synchronized boolean slow(double patience) throws InterruptedException {
            if (extra <= 0) return true;
            if (patience < extra) {
                Agent.liveWait(patience);
                a.failed = true;
                return false;
            }
            Agent.liveWait(extra);
            extra = 0;
            return true;
        }

        private synchronized int close() {
            if (closed) return real.exitValue();
            closed = true;
            try {
                byte[] o = captured(pb.redirectOutput(), in), e = pb.redirectErrorStream() ? new byte[0] : captured(pb.redirectError(), err);
                a.cut = "truncated".equals(a.fault) && o.length > 0;
                if (a.cut && pb.redirectOutput().file() != null && pb.redirectOutput() != ProcessBuilder.Redirect.DISCARD)
                    Files.write(pb.redirectOutput().file().toPath(), Arrays.copyOf(o, o.length / 2));
                Agent.Out out = Agent.Out.of(real.exitValue(), o, null);
                out.err = e;
                a.failed = out.failed("subprocess") || a.cut;
                if (entry != null) {
                    entry.put("out", out.json(r));
                    entry.put("dur", Agent.secs(System.nanoTime() - t0));
                }
            } catch (IOException | InterruptedException x) {
                Thread.currentThread().interrupt();
            }
            return real.exitValue();
        }

        private static byte[] captured(ProcessBuilder.Redirect to, Drain d) throws IOException, InterruptedException {
            return to.file() != null && to.type() == ProcessBuilder.Redirect.Type.WRITE && to != ProcessBuilder.Redirect.DISCARD
                    ? Files.readAllBytes(to.file().toPath()) : d.all();
        }

        @Override
        public int waitFor() throws InterruptedException {
            slow(Double.MAX_VALUE);
            real.waitFor();
            return close();
        }

        @Override
        public boolean waitFor(long n, TimeUnit u) throws InterruptedException {
            if (!slow(u.toNanos(n) / 1e9) || !real.waitFor(n, u)) {
                a.failed = true;
                return false;
            }
            close();
            return true;
        }

        @Override
        public int exitValue() {
            int c = real.exitValue();
            close();
            return c;
        }

        @Override
        public boolean isAlive() {
            return extra > 0 || real.isAlive();
        }

        @Override
        public void destroy() {
            extra = 0;
            real.destroy();
        }

        @Override
        public Process destroyForcibly() {
            extra = 0;
            real.destroyForcibly();
            return this;
        }

        @Override
        public long pid() {
            return real.pid();
        }

        @Override
        public CompletableFuture<Process> onExit() {
            return real.onExit().thenApply(p -> {
                try {
                    slow(Double.MAX_VALUE);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
                close();
                return this;
            });
        }

        private InputStream gate(Drain d) {
            return new InputStream() {
                @Override
                public int read() throws IOException {
                    byte[] one = new byte[1];
                    return read(one, 0, 1) < 0 ? -1 : one[0] & 255;
                }

                @Override
                public int read(byte[] b, int off, int len) throws IOException {
                    try {
                        slow(Double.MAX_VALUE);
                        int n = d.read(b, off, len);
                        if (n < 0 && len > 0 && real.waitFor(200, TimeUnit.MILLISECONDS)) Running.this.close();
                        return n;
                    } catch (InterruptedException e) {
                        throw interrupted();
                    }
                }

                @Override
                public int available() {
                    return d.available();
                }
            };
        }

        @Override
        public InputStream getInputStream() {
            return gate(in);
        }

        @Override
        public InputStream getErrorStream() {
            return gate(err);
        }

        @Override
        public OutputStream getOutputStream() {
            return real.getOutputStream();
        }
    }
}
