package com.qacer.ytblanreceiver;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.Locale;
import java.util.UUID;

final class TrinitySettings {
    static final int PORT = 1234;

    private static final String PREFS = "trinity_settings";
    private static final String KEY_AUTO_START = "auto_start_receiver";
    private static final String KEY_PAIRING_TOKEN = "pairing_token";
    private static final String KEY_BATTERY_PROMPTED = "battery_prompted";

    private TrinitySettings() {
    }

    static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static boolean isAutoStartEnabled(Context context) {
        return prefs(context).getBoolean(KEY_AUTO_START, false);
    }

    static void setAutoStartEnabled(Context context, boolean enabled) {
        prefs(context).edit().putBoolean(KEY_AUTO_START, enabled).apply();
    }

    static boolean wasBatteryPrompted(Context context) {
        return prefs(context).getBoolean(KEY_BATTERY_PROMPTED, false);
    }

    static void setBatteryPrompted(Context context, boolean prompted) {
        prefs(context).edit().putBoolean(KEY_BATTERY_PROMPTED, prompted).apply();
    }

    static String getOrCreatePairingToken(Context context) {
        SharedPreferences preferences = prefs(context);
        String existing = preferences.getString(KEY_PAIRING_TOKEN, "");
        if (existing != null && !existing.trim().isEmpty()) {
            return normalizeToken(existing);
        }

        String token = newToken();
        preferences.edit().putString(KEY_PAIRING_TOKEN, token).apply();
        return token;
    }

    static String normalizeToken(String raw) {
        if (raw == null) {
            return "";
        }

        StringBuilder out = new StringBuilder();
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            if (Character.isLetterOrDigit(c)) {
                out.append(Character.toLowerCase(c));
            }
        }
        return out.toString();
    }

    static String displayToken(String token) {
        String normalized = normalizeToken(token).toUpperCase(Locale.US);
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < normalized.length(); i++) {
            if (i > 0 && i % 8 == 0) {
                out.append(' ');
            }
            out.append(normalized.charAt(i));
        }
        return out.toString();
    }

    private static String newToken() {
        return normalizeToken(UUID.randomUUID().toString() + UUID.randomUUID().toString());
    }
}
