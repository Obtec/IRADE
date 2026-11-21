package com.endpoint.frelog.global.security;

import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.http.ResponseCookie;

import java.time.Duration;

/**
 * Stateless browser session. The login JWT itself is the session: it is stored in an HttpOnly, Secure,
 * SameSite=Strict cookie that scripts cannot read, and expires with the token (24 h). A non-secret hint
 * cookie only tells the page that a session exists so it can skip the login screen.
 */
public final class SessionCookies {
    public static final String SESSION = "IRADE_SESSION";
    public static final String HINT = "IRADE_SESSION_HINT";

    private SessionCookies() {}

    public static String read(HttpServletRequest request) {
        Cookie[] cookies = request.getCookies();
        if (cookies == null) return null;
        for (Cookie c : cookies) {
            if (SESSION.equals(c.getName()) && c.getValue() != null && !c.getValue().isBlank()) return c.getValue();
        }
        return null;
    }

    public static void write(HttpServletResponse response, String token, Duration maxAge) {
        response.addHeader("Set-Cookie", ResponseCookie.from(SESSION, token).httpOnly(true).secure(true)
                .sameSite("Strict").path("/").maxAge(maxAge).build().toString());
        response.addHeader("Set-Cookie", ResponseCookie.from(HINT, "1").httpOnly(false).secure(true)
                .sameSite("Strict").path("/").maxAge(maxAge).build().toString());
    }

    public static void clear(HttpServletResponse response) {
        write(response, "", Duration.ZERO);
    }
}
