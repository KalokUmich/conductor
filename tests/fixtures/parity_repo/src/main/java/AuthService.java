package com.example;

public class AuthService {
    public boolean authenticate(String user, String pass) {
        return user != null && pass != null;
    }
}
