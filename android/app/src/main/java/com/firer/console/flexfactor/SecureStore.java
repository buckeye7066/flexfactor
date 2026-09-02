package com.firer.console.flexfactor;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** App-private credential storage backed by a non-exportable Android Keystore key. */
final class SecureStore {
    static final String GITHUB_TOKEN = "github_token";
    static final String GITHUB_REFRESH_TOKEN = "github_refresh_token";
    static final String GITHUB_TOKEN_EXPIRES_AT = "github_token_expires_at";
    static final String DEVICE_CODE = "github_device_code";
    static final String DEVICE_USER_CODE = "github_device_user_code";
    static final String DEVICE_EXPIRES_AT = "github_device_expires_at";
    static final String DEVICE_INTERVAL = "github_device_interval";
    static final String OPENAI_KEY = "openai_key";
    static final String ANTHROPIC_KEY = "anthropic_key";
    private static final String KEY_ALIAS = "flexfactor.mobile.credentials.v1";
    private static final String PREFERENCES = "flexfactor_secure";
    private static final String TRANSFORMATION = "AES/GCM/NoPadding";

    private final SharedPreferences preferences;

    SecureStore(Context context) {
        preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE);
    }

    synchronized void put(String name, String value) throws Exception {
        String clean = value == null ? "" : value.trim();
        if (clean.isEmpty()) {
            preferences.edit().remove(name).apply();
            return;
        }
        Cipher cipher = Cipher.getInstance(TRANSFORMATION);
        cipher.init(Cipher.ENCRYPT_MODE, key());
        cipher.updateAAD(name.getBytes(StandardCharsets.UTF_8));
        byte[] encrypted = cipher.doFinal(clean.getBytes(StandardCharsets.UTF_8));
        String encoded = Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP)
                + "." + Base64.encodeToString(encrypted, Base64.NO_WRAP);
        preferences.edit().putString(name, encoded).commit();
    }

    synchronized String get(String name) {
        String encoded = preferences.getString(name, "");
        if (encoded == null || encoded.isEmpty()) return "";
        try {
            String[] parts = encoded.split("\\.", 2);
            if (parts.length != 2) throw new IllegalArgumentException("bad ciphertext");
            byte[] iv = Base64.decode(parts[0], Base64.NO_WRAP);
            byte[] encrypted = Base64.decode(parts[1], Base64.NO_WRAP);
            Cipher cipher = Cipher.getInstance(TRANSFORMATION);
            cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, iv));
            cipher.updateAAD(name.getBytes(StandardCharsets.UTF_8));
            return new String(cipher.doFinal(encrypted), StandardCharsets.UTF_8);
        } catch (Exception rejected) {
            preferences.edit().remove(name).apply();
            return "";
        }
    }

    boolean contains(String name) {
        return !get(name).isEmpty();
    }

    synchronized void clear() {
        preferences.edit().clear().commit();
    }

    private SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        java.security.Key existing = store.getKey(KEY_ALIAS, null);
        if (existing instanceof SecretKey) return (SecretKey) existing;

        KeyGenerator generator = KeyGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build());
        return generator.generateKey();
    }
}
