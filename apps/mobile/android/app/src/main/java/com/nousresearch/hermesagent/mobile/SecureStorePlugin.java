package com.nousresearch.hermesagent.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import android.util.Log;

import androidx.annotation.NonNull;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.PluginMethod;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * A deliberately small Android-keystore-backed store for the Hermes gateway
 * target. The encryption key never leaves AndroidKeyStore; SharedPreferences
 * contains only an IV plus AES-GCM ciphertext.
 */
@CapacitorPlugin(name = "SecureStore")
public class SecureStorePlugin extends Plugin {
    private static final String KEY_ALIAS = "hermes.mobile.target.v1";
    private static final String KEYSTORE = "AndroidKeyStore";
    private static final String PREFS = "hermes_mobile_secure_store";
    private static final int GCM_TAG_BITS = 128;
    private static final String LOG_TAG = "HermesSecureStore";

    @PluginMethod
    public void get(PluginCall call) {
        String key = call.getString("key");
        if (key == null || key.isEmpty()) {
            call.reject("A storage key is required.");
            return;
        }

        try {
            String stored = preferences().getString(key, null);
            JSObject result = new JSObject();
            result.put("value", stored == null ? null : decrypt(stored));
            call.resolve(result);
        } catch (Exception error) {
            rejectStorageError(call, "read", error);
        }
    }

    @PluginMethod
    public void set(PluginCall call) {
        String key = call.getString("key");
        String value = call.getString("value");
        if (key == null || key.isEmpty() || value == null) {
            call.reject("A storage key and value are required.");
            return;
        }

        try {
            if (!preferences().edit().putString(key, encrypt(value)).commit()) {
                call.reject("Could not persist secure mobile storage.");
                return;
            }
            call.resolve();
        } catch (Exception error) {
            rejectStorageError(call, "persist", error);
        }
    }

    @PluginMethod
    public void remove(PluginCall call) {
        String key = call.getString("key");
        if (key == null || key.isEmpty()) {
            call.reject("A storage key is required.");
            return;
        }

        if (!preferences().edit().remove(key).commit()) {
            call.reject("Could not clear secure mobile storage.");
            return;
        }
        call.resolve();
    }

    private void rejectStorageError(PluginCall call, String operation, Exception error) {
        // Keep token/ciphertext out of both the UI and logs. The exception class
        // is sufficient for a user to report an actionable native failure.
        Log.e(LOG_TAG, "Secure storage " + operation + " failed", error);
        call.reject("Could not " + operation + " secure mobile storage ("
                + error.getClass().getSimpleName() + ").");
    }

    private SharedPreferences preferences() {
        return getContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance(KEYSTORE);
        store.load(null);
        if (store.containsAlias(KEY_ALIAS)) {
            return ((KeyStore.SecretKeyEntry) store.getEntry(KEY_ALIAS, null)).getSecretKey();
        }

        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE);
        generator.init(new KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT
        )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                // AndroidKeyStore generates a fresh IV itself. It rejects
                // caller-provided GCM nonces for this IND-CPA-safe key.
                .setRandomizedEncryptionRequired(true)
                .setKeySize(256)
                .build());
        return generator.generateKey();
    }

    @NonNull
    private String encrypt(String value) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        // Let AndroidKeyStore choose a unique nonce. Supplying our own nonce
        // triggers InvalidAlgorithmParameterException on devices that enforce
        // randomized encryption (including the Fold 6).
        cipher.init(Cipher.ENCRYPT_MODE, key());
        byte[] iv = cipher.getIV();
        if (iv == null || iv.length == 0) {
            throw new IllegalStateException("AndroidKeyStore returned no AES-GCM IV.");
        }
        byte[] ciphertext = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
        return Base64.encodeToString(iv, Base64.NO_WRAP) + "." + Base64.encodeToString(ciphertext, Base64.NO_WRAP);
    }

    @NonNull
    private String decrypt(String stored) throws Exception {
        String[] parts = stored.split("\\.", 2);
        if (parts.length != 2) {
            throw new IllegalArgumentException("Invalid secure storage payload.");
        }
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(
                Cipher.DECRYPT_MODE,
                key(),
                new GCMParameterSpec(GCM_TAG_BITS, Base64.decode(parts[0], Base64.NO_WRAP))
        );
        return new String(cipher.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)), StandardCharsets.UTF_8);
    }
}
