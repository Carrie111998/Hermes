package com.nousresearch.hermesagent.mobile;

import android.content.ClipData;
import android.content.ContentResolver;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.provider.OpenableColumns;
import android.util.Base64;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

/**
 * Receives only user-selected Android share intents. It exposes metadata plus
 * opaque ids to JavaScript; the source content URI never crosses the bridge.
 * A shared item can be read exactly once and is capped at 25 MiB.
 */
@CapacitorPlugin(name = "InboundShare")
public class InboundSharePlugin extends Plugin {
    private static final int MAX_SHARED_ITEM_BYTES = 25 * 1024 * 1024;
    private final Map<String, Uri> pendingItems = new LinkedHashMap<>();
    private JSObject pendingShare = new JSObject();

    @Override
    protected void handleOnNewIntent(Intent intent) {
        captureShareIntent(intent);
    }

    @PluginMethod
    public void getPending(PluginCall call) {
        JSObject result = pendingShare;
        pendingShare = new JSObject();
        call.resolve(result);
    }

    @PluginMethod
    public void readItem(PluginCall call) {
        String id = call.getString("id");
        if (id == null || id.isEmpty()) {
            call.reject("A shared item id is required.");
            return;
        }

        Uri uri = pendingItems.remove(id);
        if (uri == null) {
            call.reject("Shared file is unavailable.");
            return;
        }

        try {
            byte[] bytes = readSharedBytes(uri);
            JSObject result = new JSObject();
            result.put("base64", Base64.encodeToString(bytes, Base64.NO_WRAP));
            result.put("mimeType", mimeTypeFor(uri));
            result.put("name", displayNameFor(uri));
            call.resolve(result);
        } catch (Exception error) {
            call.reject("Could not read the shared file.");
        }
    }

    private void captureShareIntent(Intent intent) {
        if (intent == null) {
            return;
        }

        String action = intent.getAction();
        if (!Intent.ACTION_SEND.equals(action) && !Intent.ACTION_SEND_MULTIPLE.equals(action)) {
            return;
        }

        JSArray items = new JSArray();
        Map<String, Boolean> seenUris = new LinkedHashMap<>();
        addShareUri(intent.getParcelableExtra(Intent.EXTRA_STREAM), items, seenUris);

        ClipData clipData = intent.getClipData();
        if (clipData != null) {
            for (int index = 0; index < clipData.getItemCount(); index += 1) {
                ClipData.Item item = clipData.getItemAt(index);
                if (item != null) {
                    addShareUri(item.getUri(), items, seenUris);
                }
            }
        }

        String text = intent.getStringExtra(Intent.EXTRA_TEXT);
        if ((text == null || text.trim().isEmpty()) && items.length() == 0) {
            return;
        }

        JSObject result = new JSObject();
        if (text != null && !text.trim().isEmpty()) {
            result.put("text", text);
        }
        if (items.length() > 0) {
            result.put("items", items);
        }

        pendingShare = result;
        notifyListeners("shareReceived", result, true);
    }

    private void addShareUri(Uri uri, JSArray items, Map<String, Boolean> seenUris) {
        if (uri == null || seenUris.containsKey(uri.toString())) {
            return;
        }

        seenUris.put(uri.toString(), true);
        String id = UUID.randomUUID().toString();
        pendingItems.put(id, uri);

        JSObject item = new JSObject();
        item.put("id", id);
        item.put("mimeType", mimeTypeFor(uri));
        item.put("name", displayNameFor(uri));
        items.put(item);
    }

    private byte[] readSharedBytes(Uri uri) throws Exception {
        ContentResolver resolver = getContext().getContentResolver();
        try (InputStream input = resolver.openInputStream(uri); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            if (input == null) {
                throw new IllegalStateException("Shared URI has no readable stream.");
            }

            byte[] buffer = new byte[8192];
            int total = 0;
            int read;
            while ((read = input.read(buffer)) != -1) {
                total += read;
                if (total > MAX_SHARED_ITEM_BYTES) {
                    throw new IllegalArgumentException("Shared file exceeds the mobile attachment limit.");
                }
                output.write(buffer, 0, read);
            }
            return output.toByteArray();
        }
    }

    private String displayNameFor(Uri uri) {
        ContentResolver resolver = getContext().getContentResolver();
        try (Cursor cursor = resolver.query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                int column = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (column >= 0) {
                    String name = cursor.getString(column);
                    if (name != null && !name.isEmpty()) {
                        return name;
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return "shared-file";
    }

    private String mimeTypeFor(Uri uri) {
        String mimeType = getContext().getContentResolver().getType(uri);
        return mimeType == null || mimeType.isEmpty() ? "application/octet-stream" : mimeType;
    }
}
