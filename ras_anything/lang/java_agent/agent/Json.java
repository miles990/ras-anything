package _ras_agent;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** A small strict JSON reader/writer. Integers are Long (BigInteger when larger), other numbers Double. */
public final class Json {
    private Json() {}

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.ws();
        Object v = p.value(0);
        p.ws();
        if (p.i < text.length()) throw p.bad("trailing characters");
        return v;
    }

    public static String write(Object v) {
        StringBuilder b = new StringBuilder();
        write(v, b);
        return b.toString();
    }

    /** Shorthand for an insertion-ordered object: map("a", 1, "b", 2). */
    public static Map<String, Object> map(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i + 1 < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    public static void write(Object v, StringBuilder b) {
        if (v == null) b.append("null");
        else if (v instanceof String s) quote(s, b);
        else if (v instanceof Boolean || v instanceof BigInteger || v instanceof Long || v instanceof Integer || v instanceof Short || v instanceof Byte)
            b.append(v);
        else if (v instanceof Number n) {
            double d = n.doubleValue();
            if (v instanceof BigDecimal bd) b.append(bd);
            else if (Double.isNaN(d) || Double.isInfinite(d)) b.append("null");
            else b.append(v);
        } else if (v instanceof Map<?, ?> m) {
            b.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) b.append(", ");
                first = false;
                quote(String.valueOf(e.getKey()), b);
                b.append(": ");
                write(e.getValue(), b);
            }
            b.append('}');
        } else if (v instanceof Iterable<?> l) {
            b.append('[');
            boolean first = true;
            for (Object x : l) {
                if (!first) b.append(", ");
                first = false;
                write(x, b);
            }
            b.append(']');
        } else quote(String.valueOf(v), b);
    }

    private static void quote(String s, StringBuilder b) {
        b.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> b.append("\\\"");
                case '\\' -> b.append("\\\\");
                case '\n' -> b.append("\\n");
                case '\r' -> b.append("\\r");
                case '\t' -> b.append("\\t");
                default -> {
                    if (c < 0x20 || c == 0x7f || Character.isSurrogate(c)) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
                }
            }
        }
        b.append('"');
    }

    private static final class Parser {
        final String s;
        int i;

        Parser(String s) {
            this.s = s;
        }

        IllegalArgumentException bad(String why) {
            return new IllegalArgumentException("bad JSON at " + i + ": " + why);
        }

        void ws() {
            while (i < s.length() && " \t\r\n".indexOf(s.charAt(i)) >= 0) i++;
        }

        char peek() {
            if (i >= s.length()) throw bad("unexpected end");
            return s.charAt(i);
        }

        Object value(int depth) {
            if (depth > 500) throw bad("too deep");
            char c = peek();
            if (c == '{') {
                i++;
                Map<String, Object> m = new LinkedHashMap<>();
                ws();
                if (peek() == '}') {
                    i++;
                    return m;
                }
                while (true) {
                    ws();
                    if (peek() != '"') throw bad("expected a key");
                    String k = string();
                    ws();
                    if (peek() != ':') throw bad("expected ':'");
                    i++;
                    ws();
                    m.put(k, value(depth + 1));
                    ws();
                    char d = peek();
                    i++;
                    if (d == '}') return m;
                    if (d != ',') throw bad("expected ',' or '}'");
                }
            }
            if (c == '[') {
                i++;
                List<Object> l = new ArrayList<>();
                ws();
                if (peek() == ']') {
                    i++;
                    return l;
                }
                while (true) {
                    ws();
                    l.add(value(depth + 1));
                    ws();
                    char d = peek();
                    i++;
                    if (d == ']') return l;
                    if (d != ',') throw bad("expected ',' or ']'");
                }
            }
            if (c == '"') return string();
            for (Object[] w : new Object[][] {{"true", true}, {"false", false}, {"null", null}}) {
                if (s.startsWith((String) w[0], i)) {
                    i += ((String) w[0]).length();
                    return w[1];
                }
            }
            int from = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
            String n = s.substring(from, i);
            try {
                if (n.matches("-?(0|[1-9]\\d*)")) {
                    BigInteger b = new BigInteger(n);
                    return b.bitLength() < 64 ? (Object) b.longValue() : b;
                }
                if (n.matches("-?(0|[1-9]\\d*)(\\.\\d+)?([eE][+-]?\\d+)?")) return Double.valueOf(n);
            } catch (NumberFormatException e) {
                throw bad("number");
            }
            i = from;
            throw bad("unexpected character");
        }

        String string() {
            StringBuilder b = new StringBuilder();
            i++;
            while (true) {
                char c = peek();
                i++;
                if (c == '"') return b.toString();
                if (c < 0x20) throw bad("control character in string");
                if (c != '\\') {
                    b.append(c);
                    continue;
                }
                char e = peek();
                i++;
                switch (e) {
                    case 'n' -> b.append('\n');
                    case 't' -> b.append('\t');
                    case 'r' -> b.append('\r');
                    case 'b' -> b.append('\b');
                    case 'f' -> b.append('\f');
                    case '"', '\\', '/' -> b.append(e);
                    case 'u' -> {
                        if (i + 4 > s.length()) throw bad("unexpected end");
                        try {
                            b.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        } catch (NumberFormatException x) {
                            throw bad("\\u escape");
                        }
                        i += 4;
                    }
                    default -> throw bad("escape");
                }
            }
        }
    }
}
