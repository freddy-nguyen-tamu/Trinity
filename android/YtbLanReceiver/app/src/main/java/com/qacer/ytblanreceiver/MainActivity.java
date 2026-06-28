package com.qacer.ytblanreceiver;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.method.LinkMovementMethod;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

public class MainActivity extends Activity {
    private static final int PERMISSION_REQUEST = 1001;

    private Button toggleButton;
    private TextView statusText;
    private TextView tokenText;
    private TextView detailsText;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        TrinitySettings.getOrCreatePairingToken(this);
        buildUi();
        requestRuntimePermissionsIfNeeded();
        requestBatteryOptimizationExemptionOnce();
        startReceiver();
    }

    @Override
    protected void onResume() {
        super.onResume();
        updateUi();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == PERMISSION_REQUEST) {
            startReceiver();
            updateUi();
        }
    }

    private void requestRuntimePermissionsIfNeeded() {
        List<String> permissions = new ArrayList<>();

        if (Build.VERSION.SDK_INT <= 28
                && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            permissions.add(Manifest.permission.WRITE_EXTERNAL_STORAGE);
        }

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            permissions.add(Manifest.permission.POST_NOTIFICATIONS);
        }

        if (!permissions.isEmpty()) {
            requestPermissions(permissions.toArray(new String[0]), PERMISSION_REQUEST);
        }
    }

    private void requestBatteryOptimizationExemptionOnce() {
        if (Build.VERSION.SDK_INT < 23 || TrinitySettings.wasBatteryPrompted(this)) {
            return;
        }

        try {
            PowerManager powerManager = (PowerManager) getSystemService(POWER_SERVICE);
            if (powerManager != null && powerManager.isIgnoringBatteryOptimizations(getPackageName())) {
                TrinitySettings.setBatteryPrompted(this, true);
                return;
            }

            Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
            intent.setData(Uri.parse("package:" + getPackageName()));
            TrinitySettings.setBatteryPrompted(this, true);
            startActivity(intent);
        } catch (Exception e) {
            try {
                TrinitySettings.setBatteryPrompted(this, true);
                startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
            } catch (Exception ignored) {
            }
        }
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(20), dp(28), dp(20), dp(20));
        root.setGravity(Gravity.CENTER_HORIZONTAL);

        TextView title = new TextView(this);
        title.setText("Trinity");
        title.setTextSize(30);
        title.setGravity(Gravity.CENTER);
        root.addView(title, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        TextView subtitle = new TextView(this);
        subtitle.setText("Always-on LAN receiver for laptop uploads.");
        subtitle.setTextSize(16);
        subtitle.setGravity(Gravity.CENTER);
        subtitle.setPadding(0, dp(10), 0, dp(18));
        root.addView(subtitle, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        toggleButton = new Button(this);
        toggleButton.setAllCaps(false);
        toggleButton.setTextSize(18);
        toggleButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (TrinitySettings.isAutoStartEnabled(MainActivity.this)) {
                    TrinityReceiverService.stop(MainActivity.this);
                } else {
                    startReceiver();
                }
                updateUi();
            }
        });
        root.addView(toggleButton, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(56)));

        statusText = new TextView(this);
        statusText.setTextSize(16);
        statusText.setPadding(0, dp(18), 0, dp(12));
        statusText.setMovementMethod(LinkMovementMethod.getInstance());
        root.addView(statusText, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        TextView tokenLabel = new TextView(this);
        tokenLabel.setText("Laptop pairing token");
        tokenLabel.setTextSize(14);
        root.addView(tokenLabel, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        tokenText = new TextView(this);
        tokenText.setTextSize(18);
        tokenText.setTextIsSelectable(true);
        tokenText.setPadding(0, dp(4), 0, dp(14));
        root.addView(tokenText, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        detailsText = new TextView(this);
        detailsText.setTextSize(14);
        detailsText.setPadding(0, 0, 0, dp(12));

        ScrollView scrollView = new ScrollView(this);
        scrollView.addView(detailsText);
        root.addView(scrollView, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1));

        setContentView(root);
        updateUi();
    }

    private void startReceiver() {
        TrinityReceiverService.start(this);
        TrinitySettings.setAutoStartEnabled(this, true);
    }

    private void updateUi() {
        if (toggleButton == null) {
            return;
        }

        boolean autoStart = TrinitySettings.isAutoStartEnabled(this);
        boolean running = TrinityReceiverService.isServerRunning();
        String url = TrinityUploadServer.currentLanUrl();
        String token = TrinitySettings.getOrCreatePairingToken(this);

        toggleButton.setText(autoStart ? "Stop Always-On Receiver" : "Start Always-On Receiver");
        statusText.setText(
                (autoStart ? "Auto receiver is enabled." : "Auto receiver is stopped.")
                        + "\n"
                        + (running ? "Server is running." : "Server is starting or waiting for Android.")
                        + "\n"
                        + (url == null ? "URL: connect to Wi-Fi to show phone IP." : "URL: " + url)
        );
        tokenText.setText(TrinitySettings.displayToken(token));
        detailsText.setText(
                "The laptop only needs this token once. After that, it stores the token in laptop/_trinity_android_token.json and uploads automatically when both devices are on the same Wi-Fi.\n\n"
                        + "Saved files: Music/TrinityUploads\n\n"
                        + "Android keeps this receiver alive with a foreground notification. If uploads stop after the phone sleeps, allow Trinity to ignore battery optimization in Android settings."
        );
    }

    private int dp(int value) {
        float density = getResources().getDisplayMetrics().density;
        return Math.round(value * density);
    }
}
