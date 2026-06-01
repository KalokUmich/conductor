package com.example;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

public class AuthServiceTest {

    @Test
    public void testAuthenticate() {
        AuthService svc = new AuthService();
        assertTrue(svc.authenticate("admin", "pass"));
    }

    @ParameterizedTest
    public void testAuthenticateNull() {
        AuthService svc = new AuthService();
        assertFalse(svc.authenticate(null, "pass"));
    }

    @Test
    public void testWithMock() {
        AuthService svc = mock(AuthService.class);
        when(svc.authenticate("a", "b")).thenReturn(true);
        verify(svc).authenticate("a", "b");
    }
}
