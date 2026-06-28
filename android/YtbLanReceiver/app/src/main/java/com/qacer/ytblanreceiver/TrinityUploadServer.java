package com.qacer.ytblanreceiver;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class TrinityUploadServer extends Thread {
    public interface Callback {
        void onLog(String message);
        void onStopped();
    }

    private final Context context;
    private final int port;
    private final Callback callback;
    private final ExecutorService executor = Executors.newCachedThreadPool();
    private volatile boolean running = false;
    private ServerSocket serverSocket;

    TrinityUploadServer(Context context, int port, Callback callback) {
        this.context = context.getApplicationContext();
        this.port = port;
        this.callback = callback;
    }

    boolean isRunning() {
        return running;
    }

    @Override
    public void run() {
        running = true;
        try {
            serverSocket = new ServerSocket(port);
            while (running) {
                final Socket socket = serverSocket.accept();
                executor.execute(new Runnable() {
                    @Override
                    public void run() {
                        handleSocket(socket);
                    }
                });
            }
        } catch (IOException e) {
            if (running) {
                log("Server error: " + e.getMessage());
            }
        } finally {
            running = false;
            closeQuietly(serverSocket);
            executor.shutdownNow();
            callback.onStopped();
        }
    }

    void shutdown() {
        running = false;
        closeQuietly(serverSocket);
        executor.shutdownNow();
    }

    static String currentLanUrl() {
        String ip = getLanIpAddress();
        if (ip == null) {
            return null;
        }
        return "http://" + ip + ":" + TrinitySettings.PORT + "/";
    }

    private static String getLanIpAddress() {
        try {
            for (NetworkInterface networkInterface : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!networkInterface.isUp() || networkInterface.isLoopback()) {
                    continue;
                }
                for (InetAddress address : Collections.list(networkInterface.getInetAddresses())) {
                    if (address instanceof Inet4Address && !address.isLoopbackAddress()) {
                        String host = address.getHostAddress();
                        if (host.startsWith("10.") || host.startsWith("192.168.") || host.startsWith("172.")) {
                            return host;
                        }
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    private void handleSocket(Socket socket) {
        try {
            socket.setSoTimeout(120000);
            BufferedInputStream input = new BufferedInputStream(socket.getInputStream());
            OutputStream output = new BufferedOutputStream(socket.getOutputStream());

            HttpRequest request = readRequest(input);
            if (request == null) {
                sendText(output, 400, "Bad Request", "text/plain; charset=utf-8", "Bad request");
                return;
            }

            if ("GET".equals(request.method) && ("/".equals(request.path) || "/index.html".equals(request.path))) {
                sendText(output, 200, "OK", "text/html; charset=utf-8", uploadPageHtml());
            } else if ("GET".equals(request.method) && ("/ping".equals(request.path) || "/health".equals(request.path))) {
                sendText(output, 200, "OK", "application/json; charset=utf-8", healthJson(request));
            } else if ("POST".equals(request.method) && "/upload-file".equals(request.path)) {
                if (!isAuthorized(request)) {
                    sendText(output, 401, "Unauthorized", "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"Pairing token required\"}");
                    return;
                }
                handleUpload(input, output, request);
            } else {
                sendText(output, 404, "Not Found", "text/plain; charset=utf-8", "Not found");
            }
        } catch (Exception e) {
            log("Request failed: " + e.getMessage());
        } finally {
            closeQuietly(socket);
        }
    }

    private String healthJson(HttpRequest request) {
        String url = currentLanUrl();
        return "{\"app\":\"Trinity\",\"ok\":true,"
                + "\"version\":2,"
                + "\"token_required\":true,"
                + "\"authorized\":" + isAuthorized(request) + ","
                + "\"url\":" + jsonString(url == null ? "" : url) + "}";
    }

    private boolean isAuthorized(HttpRequest request) {
        String expected = TrinitySettings.getOrCreatePairingToken(context);
        String provided = request.headers.get("x-trinity-token");
        if (provided == null || provided.trim().isEmpty()) {
            provided = request.query.get("token");
        }
        return expected.equals(TrinitySettings.normalizeToken(provided));
    }

    private HttpRequest readRequest(BufferedInputStream input) throws IOException {
        String headerText = readHeaders(input);
        if (headerText == null || headerText.trim().isEmpty()) {
            return null;
        }

        String[] lines = headerText.split("\\r?\\n");
        if (lines.length == 0) {
            return null;
        }

        String[] first = lines[0].split(" ");
        if (first.length < 2) {
            return null;
        }

        HttpRequest request = new HttpRequest();
        request.method = first[0].trim().toUpperCase(Locale.US);
        request.target = first[1].trim();

        int queryIndex = request.target.indexOf('?');
        if (queryIndex >= 0) {
            request.path = request.target.substring(0, queryIndex);
            request.query = parseQuery(request.target.substring(queryIndex + 1));
        } else {
            request.path = request.target;
            request.query = new HashMap<>();
        }

        request.headers = new HashMap<>();
        for (int i = 1; i < lines.length; i++) {
            int colon = lines[i].indexOf(':');
            if (colon <= 0) {
                continue;
            }
            String key = lines[i].substring(0, colon).trim().toLowerCase(Locale.US);
            String value = lines[i].substring(colon + 1).trim();
            request.headers.put(key, value);
        }

        return request;
    }

    private String readHeaders(InputStream input) throws IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        int previous3 = -1;
        int previous2 = -1;
        int previous1 = -1;
        int current;
        int maxHeaderBytes = 64 * 1024;

        while ((current = input.read()) != -1) {
            buffer.write(current);
            if (previous3 == '\r' && previous2 == '\n' && previous1 == '\r' && current == '\n') {
                break;
            }
            if (buffer.size() > maxHeaderBytes) {
                throw new IOException("Headers too large");
            }
            previous3 = previous2;
            previous2 = previous1;
            previous1 = current;
        }

        if (buffer.size() == 0) {
            return null;
        }
        return buffer.toString("ISO-8859-1");
    }

    private Map<String, String> parseQuery(String query) {
        Map<String, String> result = new HashMap<>();
        if (query == null || query.isEmpty()) {
            return result;
        }
        String[] parts = query.split("&");
        for (String part : parts) {
            int eq = part.indexOf('=');
            String rawKey = eq >= 0 ? part.substring(0, eq) : part;
            String rawValue = eq >= 0 ? part.substring(eq + 1) : "";
            try {
                String key = URLDecoder.decode(rawKey, "UTF-8");
                String value = URLDecoder.decode(rawValue, "UTF-8");
                result.put(key, value);
            } catch (Exception ignored) {
            }
        }
        return result;
    }

    private void handleUpload(BufferedInputStream input, OutputStream output, HttpRequest request) throws IOException {
        String lengthText = request.headers.get("content-length");
        if (lengthText == null) {
            sendText(output, 411, "Length Required", "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"Content-Length required\"}");
            return;
        }

        long contentLength;
        try {
            contentLength = Long.parseLong(lengthText.trim());
        } catch (NumberFormatException e) {
            sendText(output, 400, "Bad Request", "application/json; charset=utf-8", "{\"ok\":false,\"error\":\"Invalid Content-Length\"}");
            return;
        }

        String filename = request.query.get("filename");
        if (filename == null || filename.trim().isEmpty()) {
            filename = request.query.get("name");
        }
        filename = sanitizeFilename(filename);
        if (filename == null || filename.isEmpty()) {
            filename = "upload-" + System.currentTimeMillis() + ".mp3";
        }
        if (!filename.toLowerCase(Locale.US).endsWith(".mp3")) {
            filename = filename + ".mp3";
        }

        Uri mediaUri = null;
        SavedFile savedFile = null;
        try {
            savedFile = openSavedFile(filename, contentLength);
            mediaUri = savedFile.uri;
            copyExactly(input, savedFile.outputStream, contentLength);
            savedFile.outputStream.flush();
            savedFile.outputStream.close();
            markMediaFinished(mediaUri);

            String json = "{\"ok\":true,\"name\":" + jsonString(filename) + ",\"bytes\":" + contentLength + "}";
            sendText(output, 200, "OK", "application/json; charset=utf-8", json);
            log("Uploaded " + filename + " (" + contentLength + " bytes)");
        } catch (Exception e) {
            if (savedFile != null) {
                closeQuietly(savedFile.outputStream);
            }
            if (mediaUri != null) {
                try {
                    context.getContentResolver().delete(mediaUri, null, null);
                } catch (Exception ignored) {
                }
            }
            String json = "{\"ok\":false,\"error\":" + jsonString(e.getMessage()) + "}";
            sendText(output, 500, "Upload Failed", "application/json; charset=utf-8", json);
            log("Upload failed for " + filename + ": " + e.getMessage());
        }
    }

    private SavedFile openSavedFile(String filename, long size) throws IOException {
        if (Build.VERSION.SDK_INT >= 29) {
            ContentResolver resolver = context.getContentResolver();
            ContentValues values = new ContentValues();
            values.put(MediaStore.Audio.Media.DISPLAY_NAME, filename);
            values.put(MediaStore.Audio.Media.MIME_TYPE, "audio/mpeg");
            values.put(MediaStore.Audio.Media.RELATIVE_PATH, Environment.DIRECTORY_MUSIC + File.separator + "TrinityUploads");
            values.put(MediaStore.Audio.Media.IS_PENDING, 1);
            if (size >= 0) {
                values.put(MediaStore.Audio.Media.SIZE, size);
            }

            Uri uri = resolver.insert(MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, values);
            if (uri == null) {
                throw new IOException("Could not create MediaStore item");
            }
            OutputStream out = resolver.openOutputStream(uri);
            if (out == null) {
                throw new IOException("Could not open output stream");
            }
            return new SavedFile(uri, out);
        }

        File folder = new File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MUSIC), "TrinityUploads");
        if (!folder.exists() && !folder.mkdirs()) {
            throw new IOException("Could not create " + folder.getAbsolutePath());
        }
        File file = uniqueFile(folder, filename);
        return new SavedFile(null, new FileOutputStream(file));
    }

    private void markMediaFinished(Uri uri) {
        if (uri == null || Build.VERSION.SDK_INT < 29) {
            return;
        }
        ContentValues values = new ContentValues();
        values.put(MediaStore.Audio.Media.IS_PENDING, 0);
        context.getContentResolver().update(uri, values, null, null);
    }

    private File uniqueFile(File folder, String filename) {
        File candidate = new File(folder, filename);
        if (!candidate.exists()) {
            return candidate;
        }
        int dot = filename.lastIndexOf('.');
        String root = dot >= 0 ? filename.substring(0, dot) : filename;
        String ext = dot >= 0 ? filename.substring(dot) : "";
        int counter = 1;
        while (true) {
            File next = new File(folder, root + " (" + counter + ")" + ext);
            if (!next.exists()) {
                return next;
            }
            counter++;
        }
    }

    private void copyExactly(InputStream input, OutputStream rawOutput, long length) throws IOException {
        OutputStream output = new BufferedOutputStream(rawOutput);
        byte[] buffer = new byte[1024 * 1024];
        long remaining = length;
        while (remaining > 0) {
            int toRead = (int) Math.min(buffer.length, remaining);
            int read = input.read(buffer, 0, toRead);
            if (read == -1) {
                throw new IOException("Upload ended early");
            }
            output.write(buffer, 0, read);
            remaining -= read;
        }
        output.flush();
    }

    private String sanitizeFilename(String raw) {
        if (raw == null) {
            return "";
        }
        String filename = raw.trim();
        filename = filename.replace('\\', '_').replace('/', '_').replace(':', '_');
        filename = filename.replace('*', '_').replace('?', '_').replace('"', '_');
        filename = filename.replace('<', '_').replace('>', '_').replace('|', '_');
        while (filename.startsWith(".")) {
            filename = filename.substring(1);
        }
        if (filename.length() > 180) {
            int dot = filename.lastIndexOf('.');
            String ext = dot >= 0 ? filename.substring(dot) : "";
            String root = dot >= 0 ? filename.substring(0, dot) : filename;
            filename = root.substring(0, Math.min(root.length(), 180 - ext.length())) + ext;
        }
        return filename;
    }

    private String uploadPageHtml() {
        return "<!doctype html>"
                + "<html><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                + "<title>Trinity</title>"
                + "<style>body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:32px;background:#111;color:#fff}"
                + "main{max-width:640px;margin:auto}.button{display:block;width:100%;box-sizing:border-box;border:0;border-radius:14px;padding:18px 20px;font-size:20px;font-weight:700;background:#e53935;color:#fff}"
                + "input{box-sizing:border-box;width:100%;margin:10px 0 16px;padding:14px;border-radius:10px;border:1px solid #555;background:#1b1b1b;color:#fff;font-size:16px}"
                + "#status{white-space:pre-wrap;margin-top:18px;line-height:1.45;color:#ddd}.bar{height:10px;background:#333;border-radius:999px;overflow:hidden;margin-top:16px}.fill{height:100%;width:0;background:#fff}</style></head>"
                + "<body><main><h1>Trinity</h1>"
                + "<input id=\"token\" autocomplete=\"off\" placeholder=\"Pairing token from Android app\">"
                + "<input id=\"files\" type=\"file\" accept=\"audio/mpeg,.mp3\" multiple hidden>"
                + "<button class=\"button\" id=\"pick\">Choose & Upload MP3 Files</button>"
                + "<div class=\"bar\"><div class=\"fill\" id=\"fill\"></div></div><div id=\"status\">Ready.</div>"
                + "<script>const t=document.getElementById('token'),f=document.getElementById('files'),b=document.getElementById('pick'),s=document.getElementById('status'),fill=document.getElementById('fill');"
                + "t.value=localStorage.trinityToken||'';t.oninput=()=>localStorage.trinityToken=t.value;"
                + "b.onclick=()=>f.click();f.onchange=async()=>{const files=[...f.files];if(!files.length)return;b.disabled=true;let ok=0;for(let i=0;i<files.length;i++){const file=files[i];s.textContent='Uploading '+(i+1)+'/'+files.length+'\\n'+file.name;"
                + "const r=await fetch('/upload-file?filename='+encodeURIComponent(file.name),{method:'POST',headers:{'Content-Type':'audio/mpeg','X-Trinity-Token':t.value},body:file});if(!r.ok){throw new Error(await r.text())}ok++;fill.style.width=Math.round(ok/files.length*100)+'%';}"
                + "s.textContent='Done. Uploaded '+ok+' file(s).';b.disabled=false;f.value='';};</script></main></body></html>";
    }

    private void sendText(OutputStream output, int code, String reason, String contentType, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        String header = "HTTP/1.1 " + code + " " + reason + "\r\n"
                + "Content-Type: " + contentType + "\r\n"
                + "Content-Length: " + bytes.length + "\r\n"
                + "Connection: close\r\n"
                + "Access-Control-Allow-Origin: *\r\n"
                + "\r\n";
        output.write(header.getBytes(StandardCharsets.ISO_8859_1));
        output.write(bytes);
        output.flush();
    }

    private String jsonString(String value) {
        if (value == null) {
            return "\"\"";
        }
        String escaped = value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "\\r");
        return "\"" + escaped + "\"";
    }

    private void log(String message) {
        callback.onLog(message);
    }

    private void closeQuietly(Object object) {
        try {
            if (object instanceof ServerSocket) {
                ((ServerSocket) object).close();
            } else if (object instanceof Socket) {
                ((Socket) object).close();
            } else if (object instanceof OutputStream) {
                ((OutputStream) object).close();
            }
        } catch (Exception ignored) {
        }
    }

    private static class HttpRequest {
        String method;
        String target;
        String path;
        Map<String, String> query;
        Map<String, String> headers;
    }

    private static class SavedFile {
        final Uri uri;
        final OutputStream outputStream;

        SavedFile(Uri uri, OutputStream outputStream) {
            this.uri = uri;
            this.outputStream = outputStream;
        }
    }
}
