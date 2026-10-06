package ru.proyavka.cam;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLSocketFactory;

/** Поиск новых JPEG на карте и отправка на приёмник. Без Android UI, чтобы логику можно было гонять в dalvikvm. */
public final class Uploader {
    public interface Listener {
        void fileStarted(int index, int count, File f);
        void progress(long fileDone, long fileSize, long allDone, long allSize);
    }

    private final String base, token, prefix;
    private final SSLSocketFactory sf;
    private final File sentLog;
    private volatile boolean cancelled;
    private volatile HttpURLConnection current;
    private volatile java.net.Socket currentSock;

    /**
     * Отправка кадра своим HTTP-запросом поверх TLS-сокета, а не через HttpURLConnection. На Android 2.3 HttpsURLConnection
     * не передаёт setFixedLengthStreamingMode внутреннему соединению и копит весь файл в памяти — на камере OutOfMemoryError
     * на первом же кадре. Включается для SDK < 16 (MainActivity), на компьютере — -Dproyavka.raw=1.
     */
    static boolean raw = "1".equals(System.getProperty("proyavka.raw"));

    public Uploader(String base, String token, String prefix, SSLSocketFactory sf, File sentLog) {
        this.base = base.endsWith("/") ? base.substring(0, base.length() - 1) : base;
        this.token = token;
        this.prefix = prefix;
        this.sf = sf;
        this.sentLog = sentLog;
        System.setProperty("http.keepAlive", "false");   // у старого HttpURLConnection переиспользование соединений глючит
    }

    public void cancel() {
        cancelled = true;
        HttpURLConnection c = current;
        if (c != null) c.disconnect();
        java.net.Socket s = currentSock;
        if (s != null) try { s.close(); } catch (IOException e) { /* уже закрыт */ }
    }

    public boolean isCancelled() { return cancelled; }

    /** Адрес сервера (без токена) — для журнала. */
    public String base() { return base; }

    /** Ключ кадра: папка/имя + размер + время. После форматирования карты имена повторятся, но время будет другим. */
    static String key(File f) {
        return f.getParentFile().getName() + "/" + f.getName() + ":" + f.length() + ":" + f.lastModified();
    }

    public Set<String> loadSent() {
        Set<String> s = new HashSet<String>();
        if (!sentLog.exists()) return s;
        try {
            BufferedReader r = new BufferedReader(new InputStreamReader(new FileInputStream(sentLog), "UTF-8"));
            try {
                String line;
                while ((line = r.readLine()) != null) if (line.length() > 0) s.add(line);
            } finally {
                r.close();
            }
        } catch (IOException e) { /* нет журнала — отправим всё */ }
        return s;
    }

    public void markSent(File f) throws IOException {
        FileOutputStream out = new FileOutputStream(sentLog, true);
        try {
            out.write((key(f) + "\n").getBytes("UTF-8"));
            out.getFD().sync();
        } finally {
            out.close();
        }
    }

    /** Все JPEG из DCIM/*, ещё не отправленные, от старых к новым. */
    public List<File> findNew(File dcim) {
        Set<String> sent = loadSent();
        List<File> out = new ArrayList<File>();
        File[] dirs = dcim.listFiles();
        if (dirs == null) return out;
        for (File d : dirs) {
            if (!d.isDirectory()) continue;
            File[] files = d.listFiles();
            if (files == null) continue;
            for (File f : files) {
                String n = f.getName().toLowerCase();
                if (f.isFile() && (n.endsWith(".jpg") || n.endsWith(".jpeg")) && f.length() > 0 && !sent.contains(key(f))) out.add(f);
            }
        }
        Collections.sort(out, new Comparator<File>() {
            @Override public int compare(File a, File b) {
                if (a.lastModified() != b.lastModified()) return a.lastModified() < b.lastModified() ? -1 : 1;
                return a.getName().compareTo(b.getName());
            }
        });
        return out;
    }

    private HttpURLConnection open(String path) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(base + path).openConnection();
        if (c instanceof HttpsURLConnection && sf != null) ((HttpsURLConnection) c).setSSLSocketFactory(sf);
        c.setConnectTimeout(20000);
        c.setReadTimeout(60000);
        c.setUseCaches(false);
        c.setRequestProperty("X-Token", token);
        return c;
    }

    private static String readBody(HttpURLConnection c) {
        try {
            InputStream in = c.getResponseCode() >= 400 ? c.getErrorStream() : c.getInputStream();
            if (in == null) return "";
            BufferedReader r = new BufferedReader(new InputStreamReader(in, "UTF-8"));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null && sb.length() < 2000) sb.append(line);
            r.close();
            return sb.toString();
        } catch (IOException e) {
            return "";
        }
    }

    /** Проверка связи и токена до начала отправки — чтобы сразу сказать, что не так. */
    public void ping() throws IOException {
        HttpURLConnection c = open("/camera/ping");
        current = c;
        try {
            int code = c.getResponseCode();
            if (code == 401 || code == 403) throw new IOException(Cam.L("сервер не принял токен", "the server rejected the token"));
            if (code != 200) throw new IOException(Cam.L("сервер ответил ", "server responded ") + code);
        } finally {
            current = null;
            c.disconnect();
        }
    }

    private static final Pattern SHA = Pattern.compile("\"sha1\"\\s*:\\s*\"([0-9a-f]{40})\"");

    /** Отправить один файл. Успех — только если сервер вернул тот же SHA-1, что мы посчитали. */
    public void upload(File f, long allDoneBefore, long allSize, Listener l) throws IOException {
        long size = f.length();
        if (size > Integer.MAX_VALUE) throw new IOException(Cam.L("файл слишком большой", "file too large"));
        String name = prefix + f.getName();
        if (raw) { uploadRaw(f, "/camera/upload/" + URLEncoder.encode(name, "UTF-8"), size, allDoneBefore, allSize, l); return; }
        HttpURLConnection c = open("/camera/upload/" + URLEncoder.encode(name, "UTF-8"));
        current = c;
        try {
            c.setDoOutput(true);
            c.setRequestMethod("PUT");
            c.setRequestProperty("Content-Type", "image/jpeg");
            c.setFixedLengthStreamingMode((int) size);
            MessageDigest md;
            try {
                md = MessageDigest.getInstance("SHA-1");
            } catch (Exception e) {
                throw new IOException("no SHA-1");
            }
            byte[] buf = new byte[32 * 1024];
            InputStream in = new FileInputStream(f);
            long done = 0, lastReport = 0;
            try {
                OutputStream out = c.getOutputStream();
                int n;
                while ((n = in.read(buf)) > 0) {
                    if (cancelled) throw new IOException(Cam.L("отменено", "cancelled"));
                    out.write(buf, 0, n);
                    md.update(buf, 0, n);
                    done += n;
                    if (done - lastReport >= 128 * 1024 || done == size) {
                        lastReport = done;
                        if (l != null) l.progress(done, size, allDoneBefore + done, allSize);
                    }
                }
                out.close();
            } finally {
                in.close();
            }
            if (done != size) throw new IOException(Cam.L("файл изменился во время отправки", "file changed while sending"));
            int code = c.getResponseCode();
            String body = readBody(c);
            if (code != 200) throw new IOException(Cam.L("сервер ответил ", "server responded ") + code + (body.length() > 0 ? ": " + body : ""));
            Matcher m = SHA.matcher(body);
            String mine = hex(md.digest());
            if (!m.find() || !m.group(1).equals(mine)) throw new IOException(Cam.L("контрольная сумма не совпала", "checksum mismatch"));
        } finally {
            current = null;
            c.disconnect();
        }
    }

    /** PUT кадра вручную: заголовки, тело потоком по 32 КБ, ответ сервера. Сертификат — наш TrustManager, имя хоста — стандартный проверяльщик. */
    private void uploadRaw(File f, String path, long size, long allDoneBefore, long allSize, Listener l) throws IOException {
        URL url = new URL(base + path);
        boolean tls = "https".equalsIgnoreCase(url.getProtocol());
        String host = url.getHost();
        int port = url.getPort() != -1 ? url.getPort() : (tls ? 443 : 80);
        java.net.Socket plain = new java.net.Socket();
        currentSock = plain;
        try {
            plain.connect(new java.net.InetSocketAddress(host, port), 20000);
            plain.setSoTimeout(60000);
            java.net.Socket s = plain;
            if (tls) {
                javax.net.ssl.SSLSocket ss = (javax.net.ssl.SSLSocket) (sf != null ? sf : (SSLSocketFactory) SSLSocketFactory.getDefault())
                        .createSocket(plain, host, port, true);
                ss.startHandshake();
                if (!hostMatches(host, ss.getSession()))
                    throw new IOException(Cam.L("сертификат сервера не для ", "the server certificate is not for ") + host);
                s = ss;
                currentSock = ss;
            }
            MessageDigest md;
            try {
                md = MessageDigest.getInstance("SHA-1");
            } catch (Exception e) {
                throw new IOException("no SHA-1");
            }
            OutputStream out = new java.io.BufferedOutputStream(s.getOutputStream(), 32 * 1024);
            String head = "PUT " + url.getFile() + " HTTP/1.1\r\nHost: " + host + (url.getPort() != -1 ? ":" + port : "")
                    + "\r\nX-Token: " + token + "\r\nContent-Type: image/jpeg\r\nContent-Length: " + size
                    + "\r\nConnection: close\r\n\r\n";
            out.write(head.getBytes("UTF-8"));
            byte[] buf = new byte[32 * 1024];
            InputStream in = new FileInputStream(f);
            long done = 0, lastReport = 0;
            try {
                int n;
                while ((n = in.read(buf)) > 0) {
                    if (cancelled) throw new IOException(Cam.L("отменено", "cancelled"));
                    out.write(buf, 0, n);
                    md.update(buf, 0, n);
                    done += n;
                    if (done - lastReport >= 128 * 1024 || done == size) {
                        lastReport = done;
                        if (l != null) l.progress(done, size, allDoneBefore + done, allSize);
                    }
                }
                out.flush();
            } finally {
                in.close();
            }
            if (done != size) throw new IOException(Cam.L("файл изменился во время отправки", "file changed while sending"));
            InputStream rin = new java.io.BufferedInputStream(s.getInputStream());
            String status = readLine(rin);
            int code;
            try {
                code = Integer.parseInt(status.split(" ")[1]);
            } catch (Exception e) {
                throw new IOException(Cam.L("непонятный ответ сервера: ", "odd server reply: ") + status);
            }
            int length = -1;
            for (String h; (h = readLine(rin)).length() > 0; )
                if (h.toLowerCase(java.util.Locale.US).startsWith("content-length:")) length = Integer.parseInt(h.substring(15).trim());
            java.io.ByteArrayOutputStream body = new java.io.ByteArrayOutputStream();
            for (int b; body.size() < 4000 && (length < 0 || body.size() < length) && (b = rin.read()) != -1; ) body.write(b);
            String text = body.toString("UTF-8");
            if (code != 200) throw new IOException(Cam.L("сервер ответил ", "server responded ") + code + (text.length() > 0 ? ": " + text : ""));
            Matcher m = SHA.matcher(text);
            if (!m.find() || !m.group(1).equals(hex(md.digest()))) throw new IOException(Cam.L("контрольная сумма не совпала", "checksum mismatch"));
        } finally {
            currentSock = null;
            try { plain.close(); } catch (IOException e) { /* уже закрыт */ }
        }
    }

    /** Сертификат выдан на этот хост: имя из SAN (или CN, если SAN нет), маска *.домен — на один уровень. */
    static boolean hostMatches(String host, javax.net.ssl.SSLSession session) {
        try {
            java.security.cert.X509Certificate c = (java.security.cert.X509Certificate) session.getPeerCertificates()[0];
            List<String> names = new ArrayList<String>();
            java.util.Collection<List<?>> san = c.getSubjectAlternativeNames();
            if (san != null) for (List<?> e : san) if (e.size() > 1 && Integer.valueOf(2).equals(e.get(0))) names.add(String.valueOf(e.get(1)));
            if (names.isEmpty()) {
                Matcher m = Pattern.compile("(?:^|,\\s*)CN=([^,]+)").matcher(c.getSubjectX500Principal().getName());
                if (m.find()) names.add(m.group(1));
            }
            String h = host.toLowerCase(java.util.Locale.US);
            for (String n : names) {
                n = n.toLowerCase(java.util.Locale.US);
                if (n.equals(h)) return true;
                if (n.startsWith("*.") && h.endsWith(n.substring(1)) && h.indexOf('.') == h.length() - n.length() + 1) return true;
            }
            return false;
        } catch (Exception e) {
            return false;
        }
    }

    private static String readLine(InputStream in) throws IOException {
        StringBuilder sb = new StringBuilder();
        for (int b; (b = in.read()) != -1 && b != '\n'; ) if (b != '\r' && sb.length() < 8000) sb.append((char) b);
        return sb.toString();
    }

    static String hex(byte[] b) {
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format("%02x", x & 0xff));
        return sb.toString();
    }

    /** Для проверки из dalvikvm: Uploader <url> <token> <папка с pem> <DCIM> <журнал> */
    public static void main(String[] a) throws Exception {
        List<InputStream> pems = new ArrayList<InputStream>();
        File[] certs = new File(a[2]).listFiles();
        Arrays.sort(certs);
        for (File f : certs) if (f.getName().endsWith(".pem")) pems.add(new FileInputStream(f));
        Uploader u = new Uploader(a[0], a[1], "test_",Tls.factory(pems), new File(a[4]));
        u.ping();
        System.out.println("ping ok");
        List<File> files = u.findNew(new File(a[3]));
        System.out.println("new: " + files.size());
        for (File f : files) {
            long t = System.currentTimeMillis();
            u.upload(f, 0, f.length(), null);
            u.markSent(f);
            System.out.println("ok " + f + " " + f.length() / 1024 + " KB in " + (System.currentTimeMillis() - t) + " ms");
        }
    }
}
