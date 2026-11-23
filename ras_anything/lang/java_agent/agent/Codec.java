package _ras_agent;

import java.lang.reflect.Array;
import java.lang.reflect.Constructor;
import java.lang.reflect.ParameterizedType;
import java.lang.reflect.RecordComponent;
import java.lang.reflect.Type;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.net.URI;
import java.net.http.HttpClient;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * Java values to cassette JSON and back (docs/ARCHITECTURE.md, "Cassette"). Decoding goes by the declared type
 * (the method's signature, a record's components), so nothing a file names is ever loaded or constructed.
 */
final class Codec {
    private Codec() {}

    static final class Unsupported extends RuntimeException {
        Unsupported(String what) {
            super(what + " can't be stored as JSON");
        }
    }

    // ---------------------------------------------------------------- encode

    static Object encode(Object v) {
        return encode(v, 0);
    }

    private static Object encode(Object v, int depth) {
        if (depth > 60) throw new Unsupported("a value nested that deeply");
        if (v == null || v instanceof Boolean || v instanceof Integer || v instanceof Short || v instanceof Byte) return v;
        if (v instanceof Long n) return n >= Integer.MIN_VALUE && n <= Integer.MAX_VALUE ? n : Json.map("$long", n.toString());
        if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d)) return Json.map("$float", "nan");
            if (Double.isInfinite(d)) return Json.map("$float", d > 0 ? "inf" : "-inf");
            return v instanceof Float f ? (Object) Double.parseDouble(f.toString()) : v;
        }
        if (v instanceof String s) return str(s);
        if (v instanceof Character c) return str(c.toString());
        if (v instanceof Enum<?> e) return e.name();
        if (v instanceof BigDecimal || v instanceof BigInteger) return Json.map("$decimal", v.toString());
        if (v instanceof byte[] b) return Json.map("$bytes", Base64.getEncoder().encodeToString(b));
        if (v instanceof java.time.temporal.TemporalAccessor || v instanceof Duration || v instanceof java.util.UUID || v instanceof URI) return v.toString();
        if (v instanceof Optional<?> o) return o.isPresent() ? List.of(encode(o.get(), depth + 1)) : List.of();
        if (v instanceof HttpClient c)
            return Json.map("$ext", List.of("HttpClient", Json.map("connect_timeout_ms", c.connectTimeout().map(Duration::toMillis).orElse(null),
                    "redirects", c.followRedirects().name())));
        if (v.getClass().isArray()) {
            List<Object> out = new ArrayList<>();
            for (int i = 0; i < Array.getLength(v); i++) out.add(encode(Array.get(v, i), depth + 1));
            return out;
        }
        if (v instanceof Set<?> s) {
            List<Object> out = new ArrayList<>();
            for (Object x : s) out.add(encode(x, depth + 1));
            out.sort((a, b) -> Json.write(a).compareTo(Json.write(b)));
            return Json.map("$set", out);
        }
        if (v instanceof Collection<?> l) {
            List<Object> out = new ArrayList<>();
            for (Object x : l) out.add(encode(x, depth + 1));
            return out;
        }
        if (v instanceof Map<?, ?> m) {
            Map<String, Object> out = new java.util.TreeMap<>();
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!(e.getKey() instanceof String k) || k.startsWith("$")) throw new Unsupported("a map with such keys");
                out.put(k, encode(e.getValue(), depth + 1));
            }
            return out;
        }
        if (v instanceof Record r) {
            Map<String, Object> out = new LinkedHashMap<>();
            for (RecordComponent c : r.getClass().getRecordComponents()) {
                try {
                    c.getAccessor().setAccessible(true);
                    out.put(c.getName(), encode(c.getAccessor().invoke(r), depth + 1));
                } catch (ReflectiveOperationException e) {
                    throw new Unsupported(r.getClass().getName());
                }
            }
            return out;
        }
        throw new Unsupported(v.getClass().getName());
    }

    /** Text holding the project's folder is stored relocatable. */
    static Object str(String s) {
        String proj = Agent.project;
        if (proj != null && proj.length() > 3 && s.contains(proj)) {
            String mark = "${PROJECT}";
            while (s.contains(mark)) mark = "$" + mark;
            return Json.map("$proj", List.of(mark, s.replace(proj, mark)));
        }
        return s;
    }

    static String text(Object j) {
        if (j instanceof Map<?, ?> m && m.get("$proj") instanceof List<?> p)
            return ((String) p.get(1)).replace((String) p.get(0), Agent.project == null ? "." : Agent.project);
        return j == null ? null : String.valueOf(j);
    }

    // ---------------------------------------------------------------- decode

    static Object decode(Object j, Type t) {
        Class<?> c = raw(t);
        if (j == null) {
            if (c.isPrimitive() && c != void.class) throw new Unsupported("null as " + c);
            return null;
        }
        if (c == String.class) return text(j);
        if (j instanceof Map<?, ?> m && m.size() == 1) {
            if (m.get("$bytes") instanceof String b && (c == byte[].class || c == Object.class)) return Base64.getDecoder().decode(b);
            if (m.get("$ext") instanceof List<?> e && "HttpClient".equals(e.get(0)) && HttpClient.class.isAssignableFrom(c) && c.isAssignableFrom(HttpClient.class)) return client((Map<?, ?>) e.get(1));
            if (m.get("$set") instanceof List<?> l) return set(l, arg(t, 0));
            if (m.get("$proj") instanceof List<?> && c.isAssignableFrom(String.class)) return text(j);
            if (m.get("$long") instanceof String n) j = Long.valueOf(n);
            else if (m.get("$float") instanceof String f) j = f.equals("nan") ? Double.NaN : f.equals("inf") ? Double.POSITIVE_INFINITY : Double.NEGATIVE_INFINITY;
            else if (m.get("$decimal") instanceof String d) return c == BigInteger.class ? new BigInteger(d) : new BigDecimal(d);
        }
        if (c == Object.class || c == java.io.Serializable.class || c == Comparable.class) return natural(j);
        if (c == boolean.class || c == Boolean.class) return j;
        if (c == char.class || c == Character.class) return text(j).charAt(0);
        if (j instanceof Number n) {
            if (c == int.class || c == Integer.class) return n.intValue();
            if (c == long.class || c == Long.class) return n.longValue();
            if (c == double.class || c == Double.class) return n.doubleValue();
            if (c == float.class || c == Float.class) return n.floatValue();
            if (c == short.class || c == Short.class) return n.shortValue();
            if (c == byte.class || c == Byte.class) return n.byteValue();
            if (c == Number.class) return natural(j);
        }
        if (c.isEnum()) {
            for (Object k : c.getEnumConstants()) if (((Enum<?>) k).name().equals(j)) return k;
            throw new Unsupported(j + " as " + c.getName());
        }
        if (j instanceof String s) {
            if (c == java.time.Instant.class) return java.time.Instant.parse(s);
            if (c == java.time.LocalDate.class) return java.time.LocalDate.parse(s);
            if (c == java.time.LocalDateTime.class) return java.time.LocalDateTime.parse(s);
            if (c == java.time.OffsetDateTime.class) return java.time.OffsetDateTime.parse(s);
            if (c == java.time.ZonedDateTime.class) return java.time.ZonedDateTime.parse(s);
            if (c == Duration.class) return Duration.parse(s);
            if (c == java.util.UUID.class) return java.util.UUID.fromString(s);
            if (c == URI.class) return URI.create(s);
            if (c == CharSequence.class) return s;
        }
        if (j instanceof List<?> l) {
            if (c == Optional.class) return l.isEmpty() ? Optional.empty() : Optional.of(decode(l.get(0), arg(t, 0)));
            if (c.isArray()) {
                Type et = t instanceof java.lang.reflect.GenericArrayType g ? g.getGenericComponentType() : c.getComponentType();
                Object a = Array.newInstance(c.getComponentType(), l.size());
                for (int i = 0; i < l.size(); i++) Array.set(a, i, decode(l.get(i), et));
                return a;
            }
            if (c.isAssignableFrom(ArrayList.class)) {
                List<Object> out = new ArrayList<>();
                for (Object x : l) out.add(decode(x, arg(t, 0)));
                return out;
            }
        }
        if (j instanceof Map<?, ?> m) {
            if (c.isRecord()) return record(m, c);
            Map<String, Object> out = c == java.util.TreeMap.class || c == java.util.SortedMap.class ? new java.util.TreeMap<>()
                    : c.isAssignableFrom(LinkedHashMap.class) ? new LinkedHashMap<>() : null;
            if (out != null) {
                for (Map.Entry<?, ?> e : m.entrySet()) out.put((String) e.getKey(), decode(e.getValue(), arg(t, 1)));
                return out;
            }
        }
        throw new Unsupported("a " + t.getTypeName());
    }

    private static Object record(Map<?, ?> m, Class<?> c) {
        RecordComponent[] rc = c.getRecordComponents();
        Class<?>[] types = new Class<?>[rc.length];
        Object[] args = new Object[rc.length];
        for (int i = 0; i < rc.length; i++) {
            types[i] = rc[i].getType();
            args[i] = decode(m.get(rc[i].getName()), rc[i].getGenericType());
        }
        try {
            Constructor<?> k = c.getDeclaredConstructor(types);
            k.setAccessible(true);
            return k.newInstance(args);
        } catch (ReflectiveOperationException e) {
            throw new Unsupported(c.getName());
        }
    }

    private static Set<Object> set(List<?> l, Type t) {
        Set<Object> out = new LinkedHashSet<>();
        for (Object x : l) out.add(decode(x, t));
        return out;
    }

    private static HttpClient client(Map<?, ?> m) {
        HttpClient.Builder b = HttpClient.newBuilder();
        if (m.get("connect_timeout_ms") instanceof Number n) b.connectTimeout(Duration.ofMillis(n.longValue()));
        if (m.get("redirects") instanceof String r) b.followRedirects(HttpClient.Redirect.valueOf(r));
        return Agent.wrap(b.build());
    }

    private static Object natural(Object j) {
        if (j instanceof Long n && n >= Integer.MIN_VALUE && n <= Integer.MAX_VALUE) return n.intValue();
        if (j instanceof List<?> l) {
            List<Object> out = new ArrayList<>();
            for (Object x : l) out.add(decode(x, Object.class));
            return out;
        }
        if (j instanceof Map<?, ?> m) {
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<?, ?> e : m.entrySet()) out.put((String) e.getKey(), decode(e.getValue(), Object.class));
            return out;
        }
        return j;
    }

    private static Class<?> raw(Type t) {
        if (t instanceof Class<?> c) return c;
        if (t instanceof ParameterizedType p) return (Class<?>) p.getRawType();
        if (t instanceof java.lang.reflect.GenericArrayType g) return Array.newInstance(raw(g.getGenericComponentType()), 0).getClass();
        if (t instanceof java.lang.reflect.WildcardType w) return raw(w.getUpperBounds()[0]);
        return Object.class;
    }

    private static Type arg(Type t, int i) {
        return t instanceof ParameterizedType p && p.getActualTypeArguments().length > i ? p.getActualTypeArguments()[i] : Object.class;
    }
}
