package com.qacer.ytblanreceiver;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

public class TrinityReceiverService extends Service {
    static final String ACTION_START = "com.qacer.ytblanreceiver.START";
    static final String ACTION_STOP = "com.qacer.ytblanreceiver.STOP";

    private static final String CHANNEL_ID = "trinity_receiver";
    private static final int NOTIFICATION_ID = 1234;

    private static volatile boolean serverRunning = false;
    private TrinityUploadServer server;

    static void start(Context context) {
        Intent intent = new Intent(context, TrinityReceiverService.class);
        intent.setAction(ACTION_START);
        if (Build.VERSION.SDK_INT >= 26) {
            context.startForegroundService(intent);
        } else {
            context.startService(intent);
        }
    }

    static void stop(Context context) {
        Intent intent = new Intent(context, TrinityReceiverService.class);
        intent.setAction(ACTION_STOP);
        context.startService(intent);
    }

    static boolean isServerRunning() {
        return serverRunning;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            TrinitySettings.setAutoStartEnabled(this, false);
            stopReceiver();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }

        TrinitySettings.setAutoStartEnabled(this, true);
        TrinitySettings.getOrCreatePairingToken(this);
        startForeground(NOTIFICATION_ID, buildNotification());
        startReceiver();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopReceiver();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void startReceiver() {
        if (server != null && server.isRunning()) {
            return;
        }

        server = new TrinityUploadServer(getApplicationContext(), TrinitySettings.PORT, new TrinityUploadServer.Callback() {
            @Override
            public void onLog(String message) {
            }

            @Override
            public void onStopped() {
                serverRunning = false;
            }
        });
        server.start();
        serverRunning = true;
    }

    private void stopReceiver() {
        if (server != null) {
            server.shutdown();
            server = null;
        }
        serverRunning = false;
    }

    private Notification buildNotification() {
        Intent openIntent = new Intent(this, MainActivity.class);
        PendingIntent openPendingIntent = PendingIntent.getActivity(
                this,
                1,
                openIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        Intent stopIntent = new Intent(this, TrinityReceiverService.class);
        stopIntent.setAction(ACTION_STOP);
        PendingIntent stopPendingIntent = PendingIntent.getService(
                this,
                2,
                stopIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        String url = TrinityUploadServer.currentLanUrl();
        String text = url == null ? "Ready on Wi-Fi port " + TrinitySettings.PORT : url;

        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);

        builder.setContentTitle("Trinity receiver active")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setContentIntent(openPendingIntent)
                .setOngoing(true)
                .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Stop", stopPendingIntent);

        if (Build.VERSION.SDK_INT >= 21) {
            builder.setCategory(Notification.CATEGORY_SERVICE)
                    .setVisibility(Notification.VISIBILITY_PUBLIC);
        }

        return builder.build();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < 26) {
            return;
        }

        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "Trinity receiver",
                NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("Keeps the Trinity LAN receiver available on Wi-Fi.");
        NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.createNotificationChannel(channel);
        }
    }
}
