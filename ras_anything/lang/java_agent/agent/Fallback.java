package _ras_agent;

import java.lang.reflect.Field;
import java.lang.reflect.Method;

/** RAS_FALLBACK of a hardened class: a static field, or a static method with the function's parameters. */
final class Fallback {
    private Fallback() {}

    static Field field(Class<?> c) {
        try {
            Field f = c.getDeclaredField("RAS_FALLBACK");
            f.setAccessible(true);
            return f;
        } catch (NoSuchFieldException e) {
            return null;
        }
    }

    static Method method(Class<?> c) {
        for (Method m : c.getDeclaredMethods())
            if (m.getName().equals("RAS_FALLBACK")) {
                m.setAccessible(true);
                return m;
            }
        return null;
    }

    static String describe(Class<?> c) throws ReflectiveOperationException {
        if (field(c) != null) return String.valueOf(field(c).get(null));
        return method(c) != null ? "callable" : null;
    }

    static boolean matches(Class<?> c, Object[] args, Object got) {
        try {
            Object want;
            if (field(c) != null) want = field(c).get(null);
            else if (method(c) != null) want = method(c).invoke(null, args);
            else return false;
            return same(Codec.encode(want), Codec.encode(got));
        } catch (ReflectiveOperationException | RuntimeException e) {
            return false;
        }
    }

    /** Equal as cassette JSON. */
    static boolean same(Object a, Object b) {
        return java.util.Objects.equals(Json.parse(Json.write(a)), Json.parse(Json.write(b)));
    }
}
