package com.endpoint.frelog.global.security;

import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.mock;

class WorkspaceCorsTest {
    @Test void isolatedExtensionHostOriginsMustMatchTheConfiguredHostSchemeAndPort() {
        var config = new SecurityConfig(mock(JwtAuthenticationFilter.class), "https://irade.example.com", "https://*.ide.example.com:8080")
                .corsConfigurationSource().getCorsConfiguration(new MockHttpServletRequest("POST", "/api/workspaces/1/access"));
        assertNotNull(config);
        assertEquals("https://v--abc.ide.example.com:8080", config.checkOrigin("https://v--abc.ide.example.com:8080"));
        assertEquals("https://irade.example.com", config.checkOrigin("https://irade.example.com"));
        for (String origin : new String[]{"http://v--abc.ide.example.com:8080", "https://v--abc.ide.example.com:9999", "https://evil.example.com"})
            assertNull(config.checkOrigin(origin));
    }

    @Test void defaultDoesNotAllowArbitrarySubdomains() {
        var config = new SecurityConfig(mock(JwtAuthenticationFilter.class), "https://irade.example.com", "")
                .corsConfigurationSource().getCorsConfiguration(new MockHttpServletRequest("GET", "/api/workspaces"));
        assertNotNull(config);
        assertNull(config.checkOrigin("https://v--abc.ide.example.com"));
    }
}
