package com.saferoute.app;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.webkit.GeolocationPermissions;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.Toast;

/**
 * Opens the SafeRoute server in a full-screen WebView.
 *
 * Works on Android 6.0 (API 23) and newer. Location is asked for at start so "Use my location"
 * works. Phone links (tel:) open the dialler.
 *
 * The server link can change (a public tunnel gets a new address each time it restarts). When the
 * server cannot be reached, the app asks for the current link and remembers it, so the app keeps
 * working without being rebuilt or reinstalled.
 */
public class MainActivity extends Activity {

    private static final int LOCATION_REQUEST = 1;
    private static final String PREFS = "saferoute";
    private static final String KEY_URL = "server_url";

    private WebView webView;
    private SharedPreferences prefs;
    private AlertDialog linkDialog;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedState) {
        super.onCreate(savedState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        webView = new WebView(this);
        setContentView(webView);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setGeolocationEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                if (url.startsWith("tel:")) {
                    startActivity(new Intent(Intent.ACTION_DIAL, Uri.parse(url)));
                    return true;
                }
                return false; // everything else stays inside the app
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    showOffline();
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
                boolean granted = hasLocationPermission();
                callback.invoke(origin, granted, false);
                if (!granted) {
                    requestLocationPermission();
                }
            }
        });

        if (!hasLocationPermission()) {
            requestLocationPermission();
        }

        if (savedState != null) {
            webView.restoreState(savedState);
        } else {
            webView.loadUrl(serverUrl());
        }
    }

    /** The saved server link, or the one built into the app. */
    private String serverUrl() {
        String saved = prefs.getString(KEY_URL, null);
        // Temporary tunnel links from older versions expire; use the permanent link instead.
        if (saved == null || saved.contains("trycloudflare.com")) {
            return getString(R.string.saferoute_url);
        }
        return saved;
    }

    /** Turns what someone typed into a usable link, or null if it is not one. */
    private static String cleanLink(String typed) {
        String link = typed.trim();
        if (link.isEmpty()) {
            return null;
        }
        if (!link.startsWith("http://") && !link.startsWith("https://")) {
            link = "https://" + link;
        }
        Uri uri = Uri.parse(link);
        if (uri.getHost() == null || uri.getHost().isEmpty()) {
            return null;
        }
        return link.endsWith("/") ? link : link + "/";
    }

    private boolean hasLocationPermission() {
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
                || checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    private void requestLocationPermission() {
        requestPermissions(
                new String[] {Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION},
                LOCATION_REQUEST);
    }

    private void showOffline() {
        String html = "<html><body style='font-family:sans-serif;padding:24px;color:#0f172a'>"
                + "<h2>" + getString(R.string.offline_title) + "</h2>"
                + "<p>" + getString(R.string.offline_text) + "</p>"
                + "</body></html>";
        webView.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
        askForLink();
    }

    /** Ask for the current server link: retry the saved one, or open and remember a new one. */
    private void askForLink() {
        if (isFinishing() || (linkDialog != null && linkDialog.isShowing())) {
            return;
        }
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(serverUrl());
        input.setSelectAllOnFocus(true);

        linkDialog = new AlertDialog.Builder(this)
                .setTitle(R.string.link_title)
                .setMessage(R.string.link_text)
                .setView(input)
                .setCancelable(false)
                .setPositiveButton(R.string.link_open, (dialog, which) -> {
                    String link = cleanLink(input.getText().toString());
                    if (link == null) {
                        Toast.makeText(this, R.string.link_invalid, Toast.LENGTH_LONG).show();
                        webView.post(this::askForLink);
                        return;
                    }
                    prefs.edit().putString(KEY_URL, link).apply();
                    webView.loadUrl(link);
                })
                .setNegativeButton(R.string.try_again, (dialog, which) -> webView.loadUrl(serverUrl()))
                .create();
        linkDialog.show();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        webView.saveState(outState);
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
