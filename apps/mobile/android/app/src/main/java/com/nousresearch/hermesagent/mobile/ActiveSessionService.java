package com.nousresearch.hermesagent.mobile;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;

/**
 * A visible, user-controlled foreground service for an already connected remote
 * Hermes session. It is armed by the renderer, starts only after the Activity
 * moves to the background, and stops if the user dismisses the task.
 *
 * The service does not run a local agent or fabricate push delivery. It provides
 * Android's supported active-session notification and keeps Wi-Fi from entering
 * power-save while the app process is still eligible to resume its remote view.
 */
public class ActiveSessionService extends Service {
    private static final String CHANNEL_ID = "hermes_active_session";
    private static final int NOTIFICATION_ID = 4101;
    private static final String PREFS = "hermes_mobile_active_session";
    private static final String ENABLED_KEY = "enabled";

    private WifiManager.WifiLock wifiLock;

    public static void arm(Context context, boolean enabled) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putBoolean(ENABLED_KEY, enabled)
                .apply();
    }

    public static boolean isArmed(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getBoolean(ENABLED_KEY, false);
    }

    public static boolean canShowForegroundNotification(Context context) {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU
                || context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                == PackageManager.PERMISSION_GRANTED;
    }

    public static void startForBackground(Context context) {
        if (!isArmed(context) || !canShowForegroundNotification(context)) {
            return;
        }

        Intent service = new Intent(context, ActiveSessionService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(service);
        } else {
            context.startService(service);
        }
    }

    public static void stopForForeground(Context context) {
        context.stopService(new Intent(context, ActiveSessionService.class));
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
        try {
            WifiManager wifi = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if (wifi != null) {
                int mode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                        ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                        : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
                wifiLock = wifi.createWifiLock(mode, "hermes:active-session");
                wifiLock.setReferenceCounted(false);
            }
        } catch (Exception ignored) {
            // The visible service stays useful even if a device declines the Wi-Fi lock.
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(NOTIFICATION_ID, notification());
        try {
            if (wifiLock != null && !wifiLock.isHeld()) {
                wifiLock.acquire();
            }
        } catch (Exception ignored) {
        }
        // If Android removes this service, never silently revive it later.
        return START_NOT_STICKY;
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        arm(this, false);
        stopSelf();
    }

    @Override
    public void onDestroy() {
        try {
            if (wifiLock != null && wifiLock.isHeld()) {
                wifiLock.release();
            }
        } catch (Exception ignored) {
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "Hermes active session",
                NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("Visible status while an active Hermes session is in the background.");
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null) {
            manager.createNotificationChannel(channel);
        }
    }

    private Notification notification() {
        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        Intent open = new Intent(this, MainActivity.class)
                .setAction(Intent.ACTION_MAIN)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        Intent newTask = new Intent(this, MainActivity.class)
                .setAction(HermesWidgetProvider.ACTION_NEW_TASK)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE;
        return builder
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle("Hermes active session")
                .setContentText("Keeping your remote session ready")
                .setContentIntent(PendingIntent.getActivity(this, 4101, open, flags))
                .addAction(android.R.drawable.ic_input_add, "New task", PendingIntent.getActivity(this, 4102, newTask, flags))
                .setCategory(Notification.CATEGORY_SERVICE)
                .setOngoing(true)
                .setShowWhen(false)
                .build();
    }
}
