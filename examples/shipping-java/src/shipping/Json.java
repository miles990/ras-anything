package shipping;

import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.Map;

/** Just enough JSON for the carrier's answers: one flat object of strings and integers. */
final class Json {
    private Json() {}

    static Map<String, Object> object(String s) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        int i = expect(s, 0, '{');
        while (true) {
            int k = expect(s, i, '"'), end = s.indexOf('"', k);
            if (end < 0) throw new IOException("bad JSON: unterminated string");
            String key = s.substring(k, end);
            i = skip(s, expect(s, end + 1, ':'));
            if (i < s.length() && s.charAt(i) == '"') {
                end = s.indexOf('"', i + 1);
                if (end < 0) throw new IOException("bad JSON: unterminated string");
                m.put(key, s.substring(i + 1, end));
                i = end + 1;
            } else {
                end = i;
                while (end < s.length() && Character.isDigit(s.charAt(end))) end++;
                if (end == i) throw new IOException("bad JSON: expected a value");
                m.put(key, Long.parseLong(s.substring(i, end)));
                i = end;
            }
            i = skip(s, i);
            if (i < s.length() && s.charAt(i) == ',') {
                i++;
                continue;
            }
            expect(s, i, '}');
            return m;
        }
    }

    private static int skip(String s, int i) {
        while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++;
        return i;
    }

    private static int expect(String s, int i, char c) throws IOException {
        i = skip(s, i);
        if (i >= s.length() || s.charAt(i) != c) throw new IOException("bad JSON: expected '" + c + "'");
        return i + 1;
    }
}
