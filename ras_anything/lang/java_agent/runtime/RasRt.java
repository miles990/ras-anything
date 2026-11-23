package _ras_hardened;

/**
 * What hardened Java code may use: policies (deadline, attempt timeout, bounded jittered retry, retry budget, circuit
 * breaker, idempotency key), structured errors (RFC 9457), OpenTelemetry-shaped JSON logs with masking, a human gate,
 * health and incidents. Standard library only; vendored by `ras-anything enable`.
 */
public final class RasRt {
    private RasRt() {}

    // Generated from ras_anything.contracts.SCHEMA_VERSION by write_schema_version(). Do not edit by hand.
    public static final String SCHEMA_VERSION = "1.1.0";

    // ---------------------------------------------------------------- masking

    private static java.util.regex.Pattern rx(String s) {
        return java.util.regex.Pattern.compile(s);
    }

    private static final java.util.regex.Pattern EMAIL = rx("[\\w.+-]+@[\\w-]+(?:\\.[\\w-]+)+"), AUTH = rx("(?i)\\b(bearer|basic)\\s+[A-Za-z0-9._~+/=-]{6,}"),
            JWT = rx("\\beyJ[\\w-]{8,}\\.[\\w-]+\\.[\\w-]*"), PREFIXED = rx("\\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abprs]|AKIA|ASIA|AIza)[-_A-Za-z0-9]{12,}"),
            SECRET = rx("(?i)\\b(api[_-]?key|access[_-]?key|token|secret|passw(?:or)?d|pwd|authorization)\\s*([=:])\\s*([^\\s\"'&,;}]+)"),
            CARD = rx("\\b\\d(?:[ -]?\\d){12,18}\\b"),
            PHONE = rx("(?:(?:\\+\\d{1,3}[\\s.-]?)?\\(?\\d{3}\\)?[\\s.-]\\d{3}[\\s.-]\\d{4}|\\+\\d{1,3}[\\s.-]\\d{2,4}[\\s.-]\\d{3,4}[\\s.-]\\d{3,4})\\b"),
            URL = rx("(?i)\\b[a-z][a-z0-9+.-]*://[^\\s\"'<>)\\]]+"), IPV4 = rx("\\b(?:\\d{1,3}\\.){3}\\d{1,3}(?::\\d+)?\\b"), LOCAL = rx("\\blocalhost(?::\\d+)?\\b"),
            PATH = rx("(^|[^\\w.])((?:~|\\.{1,2})?/(?:[\\w.@+-]+/)+[\\w.@+-]*)"), SOURCE = rx("\\b[\\w-]+\\.(?:py|mjs|cjs|js|ts|go|rb|java|php|cs)(?::\\d+)?\\b");

    private static boolean luhn(String s) {
        int sum = 0, n = 0;
        boolean twice = false;
        for (int i = s.length() - 1; i >= 0; i--) {
            char c = s.charAt(i);
            if (c < '0' || c > '9') continue;
            int d = c - '0';
            if (twice && (d *= 2) > 9) d -= 9;
            sum += d;
            twice = !twice;
            n++;
        }
        return n >= 13 && n <= 19 && sum % 10 == 0;
    }

    /** Emails, credentials, card and phone numbers replaced. */
    public static String mask(String s) {
        if (s == null) return null;
        s = EMAIL.matcher(s).replaceAll("[email]");
        s = AUTH.matcher(s).replaceAll("$1 [token]");
        s = JWT.matcher(s).replaceAll("[token]");
        s = PREFIXED.matcher(s).replaceAll("[secret]");
        s = SECRET.matcher(s).replaceAll("$1$2[secret]");
        s = CARD.matcher(s).replaceAll(m -> luhn(m.group()) ? "[card]" : java.util.regex.Matcher.quoteReplacement(m.group()));
        return PHONE.matcher(s).replaceAll("[phone]");
    }

    /** mask(), plus no internal URLs, hosts or paths: for what leaves the process. */
    public static String plain(String s, int limit) {
        s = URL.matcher(mask(s == null ? "" : s)).replaceAll("[url]");
        s = IPV4.matcher(s).replaceAll("[host]");
        s = LOCAL.matcher(s).replaceAll("[host]");
        s = PATH.matcher(s).replaceAll("$1[path]");
        s = SOURCE.matcher(s).replaceAll("[path]").replaceAll("\\s+", " ").trim();
        return s.length() > limit ? s.substring(0, limit - 1) + "…" : s;
    }

    // ---------------------------------------------------------------- JSON

    /** A body that doesn't parse: cut off on the way (transient), as far as anyone can tell. */
    public static final class BadBodyException extends java.io.IOException {
        public BadBodyException(String message) {
            super(message);
        }

        public BadBodyException(String message, Throwable cause) {
            super(message, cause);
        }
    }

    /** Strict JSON: Map, List, String, Long, Double, Boolean or null. Throws BadBodyException (retried by a Policy). */
    public static Object parseJson(String text) throws BadBodyException {
        int[] at = {0};
        Object v = value(text, at, 0);
        space(text, at);
        if (at[0] != text.length()) throw new BadBodyException("truncated or malformed JSON");
        return v;
    }

    private static void space(String s, int[] at) {
        while (at[0] < s.length() && " \t\r\n".indexOf(s.charAt(at[0])) >= 0) at[0]++;
    }

    private static char next(String s, int[] at) throws BadBodyException {
        space(s, at);
        if (at[0] >= s.length()) throw new BadBodyException("truncated JSON");
        return s.charAt(at[0]);
    }

    private static Object value(String s, int[] at, int depth) throws BadBodyException {
        char c = next(s, at);
        if (depth > 200) throw new BadBodyException("malformed JSON: nested too deeply");
        if (c == '{' || c == '[') {
            at[0]++;
            java.util.Map<String, Object> m = new java.util.LinkedHashMap<>();
            java.util.List<Object> l = new java.util.ArrayList<>();
            char end = c == '{' ? '}' : ']';
            if (next(s, at) == end) {
                at[0]++;
                return c == '{' ? m : l;
            }
            while (true) {
                String k = null;
                if (c == '{') {
                    if (next(s, at) != '"') throw new BadBodyException("malformed JSON: expected a key");
                    k = string(s, at);
                    if (next(s, at) != ':') throw new BadBodyException("malformed JSON: expected ':'");
                    at[0]++;
                }
                Object v = value(s, at, depth + 1);
                if (c == '{') m.put(k, v);
                else l.add(v);
                char d = next(s, at);
                at[0]++;
                if (d == end) return c == '{' ? m : l;
                if (d != ',') throw new BadBodyException("malformed JSON: expected ','");
            }
        }
        if (c == '"') return string(s, at);
        for (String w : new String[] {"true", "false", "null"})
            if (s.startsWith(w, at[0])) {
                at[0] += w.length();
                return w.equals("null") ? null : (Object) w.equals("true");
            }
        int from = at[0];
        while (at[0] < s.length() && "+-0123456789.eE".indexOf(s.charAt(at[0])) >= 0) at[0]++;
        String n = s.substring(from, at[0]);
        try {
            return n.matches("-?\\d{1,18}") ? (Object) Long.valueOf(n) : (Object) Double.valueOf(n);
        } catch (NumberFormatException e) {
            throw new BadBodyException("malformed JSON");
        }
    }

    private static String string(String s, int[] at) throws BadBodyException {
        StringBuilder b = new StringBuilder();
        int i = at[0] + 1;
        while (i < s.length()) {
            char c = s.charAt(i++);
            if (c == '"') {
                at[0] = i;
                return b.toString();
            }
            if (c != '\\') {
                b.append(c);
                continue;
            }
            if (i >= s.length()) break;
            char e = s.charAt(i++);
            if (e == 'u') {
                if (i + 4 > s.length()) break;
                try {
                    b.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                } catch (NumberFormatException x) {
                    throw new BadBodyException("malformed JSON");
                }
                i += 4;
            } else {
                int k = "nrtbf".indexOf(e);
                b.append(k < 0 ? e : "\n\r\t\b\f".charAt(k));
            }
        }
        throw new BadBodyException("truncated JSON");
    }

    public static String toJson(Object v) {
        StringBuilder b = new StringBuilder();
        json(v, b);
        return b.toString();
    }

    private static void json(Object v, StringBuilder b) {
        if (v == null || v instanceof Boolean || (v instanceof Number n && !Double.isNaN(n.doubleValue()) && !Double.isInfinite(n.doubleValue()))) b.append(v);
        else if (v instanceof java.util.Map<?, ?> m) {
            String sep = "{";
            for (java.util.Map.Entry<?, ?> e : m.entrySet()) {
                b.append(sep);
                json(String.valueOf(e.getKey()), b);
                b.append(": ");
                json(e.getValue(), b);
                sep = ", ";
            }
            b.append(m.isEmpty() ? "{}" : "}");
        } else if (v instanceof Iterable<?> l) {
            String sep = "[";
            for (Object x : l) {
                b.append(sep);
                json(x, b);
                sep = ", ";
            }
            b.append(sep.equals("[") ? "[]" : "]");
        } else {
            b.append('"');
            String s = v.toString();
            for (int i = 0; i < s.length(); i++) {
                char c = s.charAt(i);
                if (c == '"' || c == '\\') b.append('\\').append(c);
                else if (c == '\n') b.append("\\n");
                else if (c < 0x20 || Character.isSurrogate(c)) b.append(String.format("\\u%04x", (int) c));
                else b.append(c);
            }
            b.append('"');
        }
    }

    /** map("ras.dependency", "rates", "n", 2): attributes for logEvent. */
    public static java.util.Map<String, Object> map(Object... kv) {
        java.util.Map<String, Object> m = new java.util.LinkedHashMap<>();
        for (int i = 0; i + 1 < kv.length; i += 2) m.put(String.valueOf(kv[i]), kv[i + 1]);
        return m;
    }

    // ---------------------------------------------------------------- trace context

    private static final java.security.SecureRandom IDS = new java.security.SecureRandom();

    private static String hex(byte[] b) {
        StringBuilder s = new StringBuilder();
        for (byte x : b) s.append(Character.forDigit((x >> 4) & 15, 16)).append(Character.forDigit(x & 15, 16));
        return s.toString();
    }

    private static String id(int bytes) {
        byte[] b = new byte[bytes];
        IDS.nextBytes(b);
        return hex(b);
    }

    private static final class Ctx {
        String fid, trace, span = id(8), gaveUp, dependency;
        String[][] deps;
        long started = System.nanoTime();
        int dropped;
        final java.util.List<Object> events = new java.util.ArrayList<>();

        void note(String event, Object... facts) {
            if (events.size() >= 200) {
                dropped++;
                return;
            }
            java.util.Map<String, Object> e = map("t", Math.round((System.nanoTime() - started) / 1e6) / 1e3, "event", event);
            e.putAll(map(facts));
            events.add(e);
        }
    }

    private static final ThreadLocal<java.util.ArrayDeque<Ctx>> CTX = ThreadLocal.withInitial(java.util.ArrayDeque::new);

    private static Ctx ctx() {
        return CTX.get().peek();
    }

    /** The active W3C trace id, or a fresh one. */
    public static String traceId() {
        if (ctx() != null) return ctx().trace;
        String tp = System.getenv("TRACEPARENT");
        String[] p = tp == null ? new String[0] : tp.split("-");
        return p.length >= 3 && p[1].length() == 32 ? p[1] : id(16);
    }

    public static String traceparent() {
        return "00-" + traceId() + "-" + (ctx() == null ? id(8) : ctx().span) + "-01";
    }

    /** RAS_ANYTHING_DISABLE=1 runs the originals. */
    public static boolean disabled(String fid) {
        return "1".equals(System.getenv("RAS_ANYTHING_DISABLE"));
    }

    // ---------------------------------------------------------------- logs

    private static String service() {
        String s = System.getenv("OTEL_SERVICE_NAME");
        return s != null ? s : "java-app";
    }

    /** One OpenTelemetry log record (JSON) on stderr. */
    public static void emit(String event, String severity, String body, java.util.Map<String, ?> attrs) {
        emit(event, severity, body, attrs, false);
    }

    /** One decision of the hardened code, as a log record; give-ups and fallbacks must be logged. */
    public static void logEvent(String event, java.util.Map<String, ?> attrs) {
        emit(event, "INFO", event, attrs, true);
    }

    private static void emit(String event, String severity, String body, java.util.Map<String, ?> attrs, boolean always) {
        try {
            java.util.Map<String, Integer> nums = java.util.Map.of("DEBUG", 5, "INFO", 9, "WARN", 13, "ERROR", 17);
            String level = System.getenv().getOrDefault("RAS_ANYTHING_LOG", "info").toUpperCase();
            int num = nums.getOrDefault(severity, 9), min = level.equals("OFF") ? 99 : nums.getOrDefault(level, 9);
            if (num < min && !always) return;
            long ms = System.currentTimeMillis();
            String trace = traceId();
            java.util.Map<String, Object> r = map("Timestamp", ms + "000000", "SeverityText", severity, "SeverityNumber", num, "EventName", event,
                    "TraceId", trace, "Body", mask(body), "Resource", map("service.name", service()));
            if (always) r.putAll(map("ts", ms / 1000.0, "event", event, "trace_id", trace));
            Ctx c = ctx();
            if (c != null) r.put("SpanId", c.span);
            java.util.Map<String, Object> a = new java.util.TreeMap<>();
            if (c != null) a.put("code.function", c.fid);
            if (attrs != null) attrs.forEach((k, v) -> {
                if (v != null && !k.equals("trace_id")) a.put(k, clean(v));
            });
            r.put("Attributes", a);
            System.err.println(toJson(r));
        } catch (RuntimeException e) {
            // a log line is never worth a call
        }
    }

    private static Object clean(Object v) {
        if (v instanceof Number || v instanceof Boolean) return v;
        if (v instanceof Throwable t) return plain(t.getClass().getSimpleName() + ": " + t.getMessage(), 200);
        if (v instanceof java.util.Map<?, ?> m) {
            java.util.Map<String, Object> o = new java.util.LinkedHashMap<>();
            m.forEach((k, x) -> o.put(String.valueOf(k), x == null ? null : clean(x)));
            return o;
        }
        return plain(v.toString(), 300);
    }

    // ---------------------------------------------------------------- errors (RFC 9457)

    /** An error with an RFC 9457 problem for whoever (or whatever agent) handles it. */
    public interface Failure {
        java.util.Map<String, Object> problem();
    }

    /** The structured error to throw when a dependency can't be used (unchecked). */
    public static final class RasError extends RuntimeException implements Failure {
        private final transient java.util.Map<String, Object> problem;

        public RasError(String message, Throwable cause, String dependency) {
            super(message, cause);
            problem = problemOf(cause, message, dependency);
        }

        @Override
        public java.util.Map<String, Object> problem() {
            return problem;
        }
    }

    /** The same as a checked java.io.IOException, for callers that catch that. */
    public static final class RasIOException extends java.io.IOException implements Failure {
        private final transient java.util.Map<String, Object> problem;

        public RasIOException(String message, Throwable cause, String dependency) {
            super(message, cause);
            problem = problemOf(cause, message, dependency);
        }

        @Override
        public java.util.Map<String, Object> problem() {
            return problem;
        }
    }

    /** An HTTP answer that isn't a result. */
    public static final class HttpStatusException extends java.io.IOException {
        public final int status;
        public final String body;
        public final Double retryAfterS;

        public HttpStatusException(int status, String body, Double retryAfterS) {
            super("HTTP " + status);
            this.status = status;
            this.body = body == null ? "" : body;
            this.retryAfterS = retryAfterS;
        }
    }

    public static final class CircuitOpenException extends RuntimeException {
        public CircuitOpenException(String name) {
            super("circuit open: " + name);
        }
    }

    /** A process that didn't finish in time, was killed, or exited 75 (try again later). */
    public static final class ProcessFailedException extends java.io.IOException {
        public final int code;
        public final boolean timedOut;

        public ProcessFailedException(String program, int code, boolean timedOut) {
            super(timedOut ? program + " timed out" : program + " exited " + code);
            this.code = code;
            this.timedOut = timedOut;
        }
    }

    private static Double retryAfter(String v) {
        try {
            return v == null ? null : Double.valueOf(v.trim());
        } catch (NumberFormatException e) {
            return null; // an HTTP date: fall back to the backoff
        }
    }

    /** The response itself if it is below 400, else an HttpStatusException. */
    public static <T> java.net.http.HttpResponse<T> check(java.net.http.HttpResponse<T> r) throws HttpStatusException {
        if (r.statusCode() < 400) return r;
        throw new HttpStatusException(r.statusCode(), r.body() instanceof String s ? s : "", retryAfter(r.headers().firstValue("Retry-After").orElse(null)));
    }

    /** Same for HttpURLConnection. */
    public static java.net.HttpURLConnection check(java.net.HttpURLConnection c) throws java.io.IOException {
        int s = c.getResponseCode();
        if (s < 400) return c;
        throw new HttpStatusException(s, "", retryAfter(c.getHeaderField("Retry-After")));
    }

    /** timeout | refused | unavailable | rate_limited | truncated | circuit_open | invalid_request | auth | conflict | quota | approval | unsupported | unknown */
    public static String faultClass(Throwable e) {
        for (int i = 0; e != null && i < 8; i++, e = e.getCause()) {
            if (e instanceof ApprovalRequiredException) return "approval";
            if (e instanceof CircuitOpenException) return "circuit_open";
            if (e instanceof java.net.http.HttpTimeoutException || e instanceof java.io.InterruptedIOException || e instanceof java.util.concurrent.TimeoutException) return "timeout";
            if (e instanceof ProcessFailedException p) return p.timedOut ? "timeout" : "unavailable";
            if (e instanceof java.net.ConnectException || e instanceof java.net.UnknownHostException || e instanceof java.nio.channels.ClosedChannelException) return "refused";
            if (e instanceof BadBodyException || e instanceof java.io.EOFException) return "truncated";
            if (e instanceof java.io.FileNotFoundException) return "invalid_request";
            if (e instanceof HttpStatusException h) return status(h.status, h.body);
            String m = String.valueOf(e.getMessage()).toLowerCase();
            java.util.regex.Matcher x = rx("response code: (\\d{3})").matcher(m);
            if (x.find()) return status(Integer.parseInt(x.group(1)), "");
            if (m.contains("error=11") || m.contains("temporarily unavailable") || m.contains("connection refused")) return "refused";
            if (m.contains("cannot run program")) return "unknown";
            if (m.contains("timed out")) return "timeout";
            if (m.contains("content-length") || m.contains("premature") || m.contains("eof")) return "truncated";
            if (e instanceof java.net.SocketException || (e instanceof java.io.IOException && (m.contains("reset") || m.contains("closed") || m.contains("no bytes")))) return "unavailable";
        }
        return "unknown";
    }

    private static String status(int s, String body) {
        if (s == 429) return body.toLowerCase().contains("quota") ? "quota" : "rate_limited";
        if (s == 408 || s == 425) return "timeout";
        if (s == 402) return "quota";
        if (s == 401 || s == 403) return "auth";
        if (s == 409) return "conflict";
        if (s == 501 || s == 505) return "unsupported";
        return s >= 500 ? "unavailable" : "invalid_request";
    }

    /** May the same request succeed later? */
    public static boolean isTransient(Throwable e) {
        return java.util.Set.of("timeout", "refused", "unavailable", "rate_limited", "truncated").contains(faultClass(e));
    }

    private static final java.util.Map<String, Object[]> CAUSES = java.util.Map.of(
            "timeout", new Object[] {"The dependency did not answer in time", 504, true, "retry_later"},
            "unavailable", new Object[] {"The dependency is unavailable", 503, true, "retry_later"},
            "rate_limited", new Object[] {"The dependency asks for fewer requests", 429, true, "retry_later"},
            "quota", new Object[] {"A quota or a budget is used up", 429, false, "escalate"},
            "invalid_request", new Object[] {"The request was refused as invalid", 400, false, "change_params"},
            "auth", new Object[] {"Not authorized at the dependency", 403, false, "escalate"},
            "conflict", new Object[] {"The request conflicts with the current state", 409, false, "stop"},
            "dependency_bug", new Object[] {"The dependency answered in a way that can't be used", 502, false, "escalate"},
            "approval_required", new Object[] {"A person has to authorize this operation", 403, false, "escalate"},
            "unknown", new Object[] {"The operation failed", 500, false, "stop"});

    /** The RFC 9457 problem of any error: masked, no internals. */
    public static java.util.Map<String, Object> problemOf(Throwable e, String detail, String dependency) {
        String f = faultClass(e), cause = switch (f) {
            case "refused", "truncated", "circuit_open" -> "unavailable";
            case "approval" -> "approval_required";
            case "unsupported" -> "dependency_bug";
            default -> CAUSES.containsKey(f) ? f : "unknown";
        };
        Object[] c = CAUSES.get(cause);
        String trace = traceId();
        Double wait = null;
        for (Throwable x = e; x != null && wait == null; x = x.getCause()) if (x instanceof HttpStatusException h) wait = h.retryAfterS;
        return map("type", "urn:ras-anything:problem:" + cause, "title", c[0], "status", c[1], "detail", plain(detail == null ? (String) c[0] : detail, 200),
                "instance", "urn:trace:" + trace, "cause", cause, "dependency", dependency == null ? null : plain(dependency, 60), "retryable", c[2],
                "retry_after_s", wait, "next_action", c[3], "needs_human_authorization", cause.equals("approval_required"), "correlation_id", trace);
    }

    // ---------------------------------------------------------------- human gate

    /** An irreversible operation nobody authorized. */
    public static final class ApprovalRequiredException extends RuntimeException implements Failure {
        private final String op;

        public ApprovalRequiredException(String op) {
            super("a person has to approve " + op + " first");
            this.op = op;
        }

        @Override
        public java.util.Map<String, Object> problem() {
            return problemOf(this, getMessage(), op);
        }
    }

    /** Passes only if RAS_ANYTHING_APPROVALS (names, or *) or .ras/approvals.json allows it. */
    public static void requireApproval(String op, java.util.Map<String, ?> details) {
        String env = System.getenv("RAS_ANYTHING_APPROVALS");
        if (env != null) for (String t : env.split(",")) if (t.trim().equals("*") || t.trim().equals(op)) return;
        try {
            if (parseJson(java.nio.file.Files.readString(java.nio.file.Path.of(".ras", "approvals.json"))) instanceof java.util.Map<?, ?> m
                    && Boolean.TRUE.equals(m.get(op))) return;
        } catch (java.io.IOException e) {
            // no file: denied
        }
        emit("ras.approval.denied", "WARN", op + " needs a person's approval", map("ras.operation", op));
        throw new ApprovalRequiredException(op);
    }

    // ---------------------------------------------------------------- circuit breaker and retry budget

    private static final java.util.List<Runnable> RESETS = java.util.Collections.synchronizedList(new java.util.ArrayList<>());

    /** Forgets breakers, budgets and statistics (between verify's trials). */
    static void resetAll() {
        synchronized (RESETS) {
            RESETS.forEach(Runnable::run);
        }
        FUNCTIONS.clear();
        OPEN.clear();
    }

    /** Fails fast while a dependency is down; lets one probe through per cooldown. */
    public static final class CircuitBreaker {
        private final String name;
        private final int threshold;
        private final long cooldown;
        private int failures;
        private boolean open, probing;
        private long openedAt;
        private final java.util.ArrayDeque<Boolean> window = new java.util.ArrayDeque<>();

        public CircuitBreaker(String name, int threshold, java.time.Duration cooldown) {
            this.name = name;
            this.threshold = threshold;
            this.cooldown = cooldown.toNanos();
            RESETS.add(() -> {
                synchronized (this) {
                    failures = 0;
                    open = probing = false;
                    window.clear();
                }
            });
        }

        public synchronized void allow() {
            if (!open) return;
            if (probing || System.nanoTime() - openedAt < cooldown) throw new CircuitOpenException(name);
            probing = true;
        }

        public synchronized void success() {
            failures = 0;
            probing = false;
            mark(true);
            if (open) {
                open = false;
                window.clear();
                OPEN.remove(name);
                emit("ras.breaker.transition", "INFO", "breaker " + name + ": closed", map("ras.breaker", name, "ras.breaker.state", "closed", "ras.dependency", name));
            }
        }

        public synchronized void failure() {
            failures++;
            mark(false);
            boolean rate = window.size() >= 20 && window.stream().filter(x -> !x).count() * 2 >= window.size();
            if (open || failures >= threshold || rate) {
                if (!open)
                    emit("ras.breaker.transition", "WARN", "breaker " + name + ": open", map("ras.breaker", name, "ras.breaker.state", "open", "ras.dependency", name));
                open = true;
                probing = false;
                openedAt = System.nanoTime();
                OPEN.put(name, "open");
            }
        }

        private void mark(boolean ok) {
            window.add(ok);
            if (window.size() > 50) window.poll();
        }

        /** The probe ended without telling anything. */
        synchronized void release() {
            probing = false;
        }

        public synchronized String state() {
            return !open ? "closed" : probing ? "half_open" : "open";
        }
    }

    /** Retries may be a tenth of the calls plus a trickle, so an outage isn't multiplied. */
    public static final class RetryBudget {
        private double tokens = 10;
        private long last = System.nanoTime();

        public RetryBudget() {
            RESETS.add(() -> {
                synchronized (this) {
                    tokens = 10;
                    last = System.nanoTime();
                }
            });
        }

        synchronized void call() {
            refill();
            tokens = Math.min(10, tokens + 0.1);
        }

        synchronized boolean take() {
            refill();
            if (tokens < 1) return false;
            tokens--;
            return true;
        }

        private void refill() {
            long now = System.nanoTime();
            tokens = Math.min(10, tokens + Math.max(0, now - last) / 1e9 * 2);
            last = now;
        }
    }

    // ---------------------------------------------------------------- policy

    /** One attempt inside a Policy: put its timeout on the request, and send its headers. */
    public static final class Attempt {
        public final int number;
        private final java.time.Duration timeout;
        private final java.util.Map<String, String> headers;

        Attempt(int number, java.time.Duration timeout, java.util.Map<String, String> headers) {
            this.number = number;
            this.timeout = timeout;
            this.headers = headers;
        }

        /** For HttpRequest.Builder.timeout, or Process.waitFor. */
        public java.time.Duration timeout() {
            return timeout;
        }

        /** For HttpURLConnection.setConnectTimeout / setReadTimeout. */
        public int timeoutMillis() {
            return (int) Math.max(1, timeout.toMillis());
        }

        /** Idempotency-Key, when the call has one. */
        public java.util.Map<String, String> headers() {
            return headers;
        }

        public java.net.http.HttpRequest.Builder apply(java.net.http.HttpRequest.Builder b) {
            headers.forEach(b::setHeader);
            return b.timeout(timeout);
        }
    }

    public interface Call<T> {
        T call(Attempt a) throws Exception;
    }

    private static final ThreadLocal<Long> DEADLINE = new ThreadLocal<>();

    /**
     * deadline > retry budget > Retry-After > retry (full jitter, transient errors only) > circuit breaker > attempt timeout.
     * Keep one per dependency in a static final field.
     */
    public static final class Policy {
        private final String name;
        private java.time.Duration deadline = java.time.Duration.ofMillis(4800), attempt = java.time.Duration.ofMillis(3500),
                base = java.time.Duration.ofMillis(100), cap = java.time.Duration.ofSeconds(2);
        private int attempts = 3;
        private CircuitBreaker breaker;
        private final RetryBudget budget = new RetryBudget();
        private java.util.function.Predicate<Throwable> retryOn = RasRt::isTransient;

        private Policy(String name) {
            this.name = name;
            breaker = new CircuitBreaker(name, 5, java.time.Duration.ofSeconds(1));
            RESETS.add(() -> good.clear());
        }

        /** The whole call, waits included (default 4.8 s). */
        public Policy deadline(java.time.Duration d) {
            deadline = d;
            return this;
        }

        /** One attempt (default 3.5 s): leave room for a second one inside the deadline. */
        public Policy attemptTimeout(java.time.Duration d) {
            attempt = d;
            return this;
        }

        public Policy attempts(int n) {
            attempts = n;
            return this;
        }

        public Policy backoff(java.time.Duration base, java.time.Duration cap) {
            this.base = base;
            this.cap = cap;
            return this;
        }

        public Policy breaker(int threshold, java.time.Duration cooldown) {
            breaker = new CircuitBreaker(name, threshold, cooldown);
            return this;
        }

        /** Also retry these (default: isTransient). */
        public Policy alsoRetry(java.util.function.Predicate<Throwable> p) {
            retryOn = retryOn.or(p);
            return this;
        }

        /** A read. */
        public <T> T call(Call<T> fn) throws Exception {
            return run("GET", null, fn);
        }

        /** A write (POST, PUT, PATCH, DELETE) is repeated only with an idempotency key; send a.headers(). Anything else is a read. */
        public <T> T call(String method, String idempotencyKey, Call<T> fn) throws Exception {
            return run(method, idempotencyKey, fn);
        }

        private final java.util.Map<String, Object[]> good = java.util.Collections.synchronizedMap(new java.util.LinkedHashMap<>() {
            @Override
            protected boolean removeEldestEntry(java.util.Map.Entry<String, Object[]> e) {
                return size() > 1000;
            }
        });

        /** A read whose last good answer will do, while it is at most maxAge old, when the dependency can't be used. */
        @SuppressWarnings("unchecked")
        public <T> T callCached(String key, java.time.Duration maxAge, Call<T> fn) throws Exception {
            try {
                T v = call(fn);
                good.put(key, new Object[] {System.nanoTime(), v});
                return v;
            } catch (RasError e) {
                Object[] old = good.get(key);
                if (old == null || System.nanoTime() - (Long) old[0] > maxAge.toNanos()) throw e;
                emit("fallback", "WARN", name + ": the last good answer instead", map("ras.dependency", name, "ras.fault", faultClass(e), "ras.fallback", "stale"), true);
                return (T) old[1];
            }
        }

        private <T> T run(String method, String key, Call<T> fn) throws Exception {
            boolean write = java.util.Set.of("POST", "PUT", "PATCH", "DELETE").contains(method);
            int max = write && key == null ? 1 : attempts;
            java.util.Map<String, String> headers = key == null ? java.util.Map.of() : java.util.Map.of("Idempotency-Key", key);
            Long outer = DEADLINE.get();
            long end = System.nanoTime() + deadline.toNanos();
            if (outer != null && outer < end) end = outer;
            Ctx c = ctx();
            String dep = dependency();
            if (c != null) c.dependency = dep;
            budget.call();
            DEADLINE.set(end);
            Exception last = null;
            try {
                for (int n = 1; ; n++) {
                    long left = end - System.nanoTime();
                    if (left < 10_000_000) throw gaveUp(n - 1, "deadline", last == null ? new java.util.concurrent.TimeoutException("no time left") : last);
                    try {
                        breaker.allow();
                    } catch (CircuitOpenException e) {
                        throw gaveUp(n - 1, "circuit open", last == null ? e : last);
                    }
                    long t0 = System.nanoTime();
                    try {
                        T v = fn.call(new Attempt(n, java.time.Duration.ofNanos(Math.min(attempt.toNanos(), left)), headers));
                        breaker.success();
                        return v;
                    } catch (InterruptedException e) {
                        breaker.release();
                        Thread.currentThread().interrupt();
                        throw e;
                    } catch (Exception e) {
                        last = e;
                    } catch (Error e) {
                        breaker.release();
                        throw e;
                    }
                    String fault = faultClass(last);
                    boolean again = retryOn.test(last) || (key != null && fault.equals("conflict")); // 409: the first attempt is still at it
                    if (again && !fault.equals("rate_limited")) breaker.failure();
                    else if (again) breaker.release();
                    else breaker.success(); // it answered
                    if (c != null) c.note("failure", "n", n, "dependency", dep, "fault", fault, "took_s", (System.nanoTime() - t0) / 1e9);
                    emit("ras.dependency.failure", "WARN", dep + " failed: " + fault + " (attempt " + n + ")", map("ras.dependency", dep, "ras.policy", name, "ras.fault", fault,
                            "error.type", last.getClass().getSimpleName(), "exception.message", last.getMessage(), "http.request.resend_count", n - 1,
                            "http.response.status_code", last instanceof HttpStatusException h ? h.status : null));
                    if (!again) {
                        note(n, "not transient", fault);
                        throw last;
                    }
                    if (n >= max) throw gaveUp(n, write && key == null ? "a write without a key isn't repeated" : "attempts", last);
                    long wait = last instanceof HttpStatusException h && h.retryAfterS != null ? (long) (h.retryAfterS * 1e9)
                            : java.util.concurrent.ThreadLocalRandom.current().nextLong(Math.min(cap.toNanos(), base.toNanos() << Math.min(n - 1, 20)) + 1);
                    if (System.nanoTime() + wait >= end - 50_000_000) throw gaveUp(n, "deadline", last);
                    if (!budget.take()) throw gaveUp(n, "retry budget", last);
                    try {
                        Thread.sleep(java.time.Duration.ofNanos(wait).toMillis() + 1);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        throw e;
                    }
                }
            } finally {
                if (outer == null) DEADLINE.remove();
                else DEADLINE.set(outer);
            }
        }

        /** The dependency as ras-anything recorded it (RUNBOOK.md uses the same names); without a recording, the policy's name. */
        private String dependency() {
            Ctx c = ctx();
            if (c == null || c.deps == null) return name;
            if (c.deps.length == 1) return c.deps[0][0];
            for (String[] d : c.deps) if (name.contains(d[0])) return d[0];
            return name;
        }

        private void note(int n, String why, String fault) {
            Ctx c = ctx();
            if (c != null) {
                c.gaveUp = fault;
                c.note("gave_up", "n", n, "why", why, "fault", fault, "dependency", dependency());
            }
        }

        private RasError gaveUp(int n, String why, Exception last) {
            String fault = faultClass(last);
            note(n, why, fault);
            emit("ras.dependency.gave_up", "ERROR", name + ": gave up after " + n + " attempt(s) (" + why + "): " + fault, map("ras.dependency", dependency(), "ras.policy", name, "ras.fault", fault,
                    "error.type", last.getClass().getSimpleName(), "exception.message", last.getMessage(), "ras.why", why), true);
            return new RasError(name + " isn't available: " + fault, last, name);
        }
    }

    public static Policy policy(String dependency) {
        return new Policy(dependency);
    }

    /** One deterministic key per logical operation, the same for every attempt. */
    public static String idempotencyKey(Object... parts) {
        try {
            StringBuilder b = new StringBuilder();
            for (Object p : parts) b.append(b.length() == 0 ? "" : "|").append(p);
            byte[] h = java.security.MessageDigest.getInstance("SHA-256").digest(b.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
            return "ras-" + hex(h).substring(0, 16);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    // ---------------------------------------------------------------- processes

    public static final class Result {
        public final int code;
        public final String stdout, stderr;

        Result(int code, String stdout, String stderr) {
            this.code = code;
            this.stdout = stdout;
            this.stderr = stderr;
        }
    }

    /** Runs a program with a timeout (killed when it passes). Exit 75 or a killed process is a ProcessFailedException; check other codes yourself. */
    public static Result run(Attempt a, String... argv) throws java.io.IOException, InterruptedException {
        return run(a.timeout(), new ProcessBuilder(argv));
    }

    public static Result run(java.time.Duration timeout, ProcessBuilder pb) throws java.io.IOException, InterruptedException {
        return run(timeout, pb, null);
    }

    /** With something to read on its standard input. */
    public static Result run(java.time.Duration timeout, ProcessBuilder pb, String stdin) throws java.io.IOException, InterruptedException {
        Process p = pb.start();
        Thread feed = new Thread(() -> {
            try (java.io.OutputStream o = p.getOutputStream()) {
                if (stdin != null) o.write(stdin.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            } catch (java.io.IOException e) {
                // it doesn't want to know
            }
        }, "ras-feed");
        feed.setDaemon(true);
        feed.start();
        String[] out = {"", ""};
        Thread[] readers = {reader(p.getInputStream(), out, 0), reader(p.getErrorStream(), out, 1)};
        try {
            if (!p.waitFor(timeout.toNanos(), java.util.concurrent.TimeUnit.NANOSECONDS)) throw new ProcessFailedException(pb.command().get(0), -1, true);
            for (Thread t : readers) t.join(1000);
            int code = p.exitValue();
            if (code == 75 || code >= 128) throw new ProcessFailedException(pb.command().get(0), code, false);
            synchronized (out) {
                return new Result(code, out[0], out[1]);
            }
        } finally {
            p.destroyForcibly();
        }
    }

    private static Thread reader(java.io.InputStream in, String[] out, int i) {
        Thread t = new Thread(() -> {
            try {
                String s = new String(in.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
                synchronized (out) {
                    out[i] = s;
                }
            } catch (java.io.IOException e) {
                // killed: what it said doesn't matter
            }
        }, "ras-reader");
        t.setDaemon(true);
        t.start();
        return t;
    }

    // ---------------------------------------------------------------- calls, health, incidents

    private static final java.util.Map<String, java.util.Map<String, Object>> FUNCTIONS = new java.util.concurrent.ConcurrentSkipListMap<>();
    private static final java.util.Map<String, String> OPEN = new java.util.concurrent.ConcurrentSkipListMap<>();
    private static long healthAt;

    static {
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            try {
                if (healthAt != 0) writeHealth(true);
            } catch (java.io.IOException | RuntimeException e) {
                // the last call's count
            }
        }, "ras-health"));
    }

    public interface Body<T> {
        T call() throws Throwable;
    }

    /** One call of an enabled function: trace context, statistics, health file, incident on failure. */
    @SuppressWarnings("unchecked")
    public static <T, E extends Throwable> T observe(String fid, String[][] deps, Body<T> body) throws E {
        Ctx c = new Ctx();
        c.fid = fid;
        c.deps = deps;
        c.trace = traceId();
        CTX.get().push(c);
        Throwable failed = null;
        try {
            return body.call();
        } catch (Throwable e) {
            failed = e;
            throw (E) e;
        } finally {
            try {
                finished(c, failed);
            } catch (RuntimeException | java.io.IOException e) {
                // bookkeeping never fails a call
            }
            CTX.get().poll();
        }
    }

    private static void finished(Ctx c, Throwable e) throws java.io.IOException {
        String fault = c.gaveUp != null ? c.gaveUp : e == null ? null : faultClass(e);
        java.util.Map<String, Object> s = FUNCTIONS.computeIfAbsent(c.fid, k -> new java.util.LinkedHashMap<>(map("calls", 0, "raised", 0, "gave_up", 0)));
        synchronized (s) {
            s.merge("calls", 1, (a, b) -> (Integer) a + 1);
            if (e != null) s.merge("raised", 1, (a, b) -> (Integer) a + 1);
            if (c.gaveUp != null) s.merge("gave_up", 1, (a, b) -> (Integer) a + 1);
            if (fault != null) s.put("last_fault", fault);
        }
        if ("1".equals(System.getenv("RAS_ANYTHING_VERIFY"))) return;
        writeHealth(fault != null);
        if (fault != null) incident(c, e, fault);
    }

    /** Functions seen and open breakers. */
    public static java.util.Map<String, Object> health() {
        java.util.Map<String, Object> f = new java.util.TreeMap<>();
        FUNCTIONS.forEach((k, v) -> {
            synchronized (v) {
                f.put(k, new java.util.LinkedHashMap<>(v));
            }
        });
        return map("status", OPEN.isEmpty() ? "ok" : "degraded", "updated", stamp(), "breakers", new java.util.TreeMap<>(OPEN), "functions", f);
    }

    private static String stamp() {
        return java.time.Instant.now().truncatedTo(java.time.temporal.ChronoUnit.SECONDS).toString();
    }

    private static synchronized void writeHealth(boolean now) throws java.io.IOException {
        String to = System.getenv("RAS_ANYTHING_HEALTH_FILE");
        java.nio.file.Path ras = java.nio.file.Path.of(".ras");
        if (to == null && !java.nio.file.Files.isDirectory(ras)) return;
        if (!now && healthAt != 0 && System.nanoTime() - healthAt < 1_000_000_000L) return;
        healthAt = System.nanoTime();
        java.nio.file.Files.writeString(to == null ? ras.resolve("health.json") : java.nio.file.Path.of(to), toJson(health()) + "\n");
    }

    private static void incident(Ctx c, Throwable e, String fault) throws java.io.IOException {
        String at = System.getenv("RAS_ANYTHING_INCIDENTS");
        if (at != null && (at.equalsIgnoreCase("off") || at.equals("0"))) return;
        if (at == null && !java.nio.file.Files.isDirectory(java.nio.file.Path.of(".ras"))) return;
        java.nio.file.Path dir = at == null ? java.nio.file.Path.of(".ras", "incidents") : java.nio.file.Path.of(at);
        String id = "inc-" + stamp().replaceAll("[-:Z]", "") + "-" + c.fid.replaceAll("[^\\w.-]", "_") + "-" + c.span.substring(0, 8);
        java.util.List<Object> deps = new java.util.ArrayList<>();
        for (String[] d : c.deps) deps.add(map("name", d[0], "kind", d[1]));
        java.util.Map<String, Object> doc = map("schema_version", SCHEMA_VERSION, "kind", "incident", "id", id, "created", stamp(), "lang", "java", "service", service(),
                "pid", ProcessHandle.current().pid(), "function", c.fid, "trigger", c.gaveUp != null ? "gave_up" : "raise", "trace_id", c.trace, "span_id", c.span,
                "duration_s", Math.round((System.nanoTime() - c.started) / 1e6) / 1e3, "dependencies", deps, "dependency", c.dependency, "fault", fault, "events", c.events);
        if (e != null)
            doc.put("outcome", map("kind", "raise", "error_type", e.getClass().getSimpleName(), "message", plain(e.getMessage(), 300),
                    "problem", e instanceof Failure f ? f.problem() : problemOf(e, e.getMessage(), c.dependency)));
        java.nio.file.Files.createDirectories(dir);
        java.nio.file.Files.writeString(dir.resolve(id + ".json"), toJson(doc) + "\n");
    }
}
