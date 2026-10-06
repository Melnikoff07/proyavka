package ru.proyavka.cam;

import android.content.Context;
import android.content.Intent;
import android.content.res.AssetManager;
import android.graphics.Color;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.net.wifi.WifiConfiguration;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.Environment;
import android.widget.TextView;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;

/**
 * Общее для экранов: папка PROYAVKA на карте, настройки, сохранённые сети Wi-Fi и мелочи камеры Sony.
 *
 * Всё хранится на карте: /data/data у камеры на RAM-диске и не переживает выключение.
 *   PROYAVKA/config.txt — адрес и токен сервера (делает мастер установки), можно и сети wifiN.ssid/pass
 *   PROYAVKA/wifi.txt   — сети, добавленные прямо в камере (экран Wi-Fi); они главнее
 *   PROYAVKA/sent.txt   — что уже отправлено
 */
final class Cam {
    private Cam() {}

    /** Wi-Fi включало приложение — значит, на выходе его надо выключить. Общее для всех экранов. */
    static volatile boolean weEnabledWifi;
    /** Камера ушла в съёмку/выключается — закрыть все экраны. */
    static volatile boolean exitAll;

    // ---------- язык: lang из config.txt, иначе язык камеры; всё, кроме русского, — английский ----------

    static volatile boolean en;

    static String L(String ru, String english) { return en ? english : ru; }

    static void initLang(Context ctx) {
        String l = null;
        try { l = config(ctx).getProperty("lang"); } catch (IOException e) { /* нет карты — язык камеры */ }
        if (l == null || l.trim().length() == 0) l = java.util.Locale.getDefault().getLanguage();
        en = !l.trim().toLowerCase().startsWith("ru");
    }

    /** «3 кадра» / «3 frames». */
    static String count(int n, String one, String few, String many, String enOne, String enMany) {
        return n + " " + (en ? (n == 1 ? enOne : enMany) : plural(n, one, few, many));
    }

    static File dir() {
        File d = new File(Environment.getExternalStorageDirectory(), "PROYAVKA");
        d.mkdirs();
        return d;
    }

    // ---------- настройки ----------

    /** Где искали config.txt и что нашли — для сообщения на экране, если настроек нет. */
    static String configWhere = "";
    static File configFound = null;

    static Properties config(Context ctx) throws IOException {
        Properties cfg = new Properties();
        try {   // config.properties в APK — для личной сборки, в общей его нет
            InputStream in = ctx.getAssets().open("config.properties");
            try { cfg.load(in); } finally { in.close(); }
        } catch (IOException e) { /* нет — и ладно */ }
        configFound = findConfig();
        if (configFound != null) load(cfg, configFound);
        return cfg;
    }

    /**
     * config.txt на карте: без учёта регистра букв (камеры на Android 2.3 пишут имена как CONFIG.TXT), а также config.txt.txt —
     * так файл сохраняет Windows со скрытыми расширениями. Кроме основной папки — запасные пути к карте на старых прошивках.
     */
    static File findConfig() {
        List<File> dirs = new ArrayList<File>();
        dirs.add(dir());
        for (String root : new String[] {"/mnt/sdcard", "/sdcard", "/storage/sdcard0", "/mnt/extSdCard", "/mnt/sdcard/external_sd"}) {
            File d = new File(root, "PROYAVKA");
            if (!dirs.contains(d)) dirs.add(d);
        }
        StringBuilder where = new StringBuilder();
        String seen = "";
        for (File d : dirs) {
            if (where.length() > 0) where.append(", ");
            where.append(d.getPath());
            File[] list = d.listFiles();
            if (list == null) continue;
            for (String want : new String[] {"config.txt", "config.txt.txt", "config"}) {
                for (File f : list) if (f.isFile() && f.getName().equalsIgnoreCase(want)) { configWhere = where.toString(); return f; }
            }
            // камеры на Android 2.3 видят карту только в коротких именах DOS 8.3: config.txt.txt там — CONFIG~1.TXT
            for (File f : list) {
                String n = f.getName().toUpperCase(java.util.Locale.US);
                if (f.isFile() && n.startsWith("CONFIG") && n.endsWith(".TXT")) { configWhere = where.toString(); return f; }
            }
            if (seen.length() == 0) {
                StringBuilder s = new StringBuilder();
                for (File f : list) s.append(s.length() > 0 ? ", " : "").append(f.getName());
                seen = d.getPath() + ": " + (s.length() > 0 ? s.toString() : "—");
            }
        }
        configWhere = where.toString() + (seen.length() > 0 ? "; files in " + seen : "");
        return null;
    }

    private static void load(Properties p, File f) throws IOException {
        if (!f.exists()) {
            for (File g : f.getParentFile() == null || f.getParentFile().listFiles() == null ? new File[0] : f.getParentFile().listFiles())
                if (g.getName().equalsIgnoreCase(f.getName())) { f = g; break; }   // WIFI.TXT вместо wifi.txt
            if (!f.exists()) return;
        }
        byte[] b = readAll(f);
        p.load(new java.io.StringReader(decode(b)));
    }

    private static byte[] readAll(File f) throws IOException {
        InputStream in = new FileInputStream(f);
        try {
            java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
            return out.toByteArray();
        } finally { in.close(); }
    }

    /** Текст в любой из кодировок, в которых его сохраняют блокноты: UTF-8 (с меткой BOM и без), UTF-16 LE/BE. */
    static String decode(byte[] b) throws IOException {
        if (b.length >= 2 && (b[0] & 0xff) == 0xff && (b[1] & 0xff) == 0xfe) return new String(b, 2, b.length - 2, "UTF-16LE");
        if (b.length >= 2 && (b[0] & 0xff) == 0xfe && (b[1] & 0xff) == 0xff) return new String(b, 2, b.length - 2, "UTF-16BE");
        int zeros = 0;
        for (int i = 1; i < Math.min(b.length, 200); i += 2) if (b[i] == 0) zeros++;
        if (b.length > 4 && zeros > Math.min(b.length, 200) / 4) return new String(b, "UTF-16LE");    // UTF-16 без метки
        String s = new String(b, "UTF-8");
        return s.length() > 0 && s.charAt(0) == '\uFEFF' ? s.substring(1) : s;
    }

    static List<InputStream> certs(Context ctx) throws IOException {
        AssetManager am = ctx.getAssets();
        List<InputStream> pems = new ArrayList<InputStream>();
        for (String n : am.list("certs")) pems.add(am.open("certs/" + n));
        return pems;
    }

    // ---------- сохранённые сети ----------

    static final class Net {
        final String ssid, pass;
        Net(String ssid, String pass) { this.ssid = ssid; this.pass = pass == null ? "" : pass; }
    }

    private static List<Net> read(Properties p) {
        List<Net> l = new ArrayList<Net>();
        for (int i = 1; i <= 20; i++) {
            String s = p.getProperty("wifi" + i + ".ssid", "").trim();
            if (s.length() > 0) l.add(new Net(s, p.getProperty("wifi" + i + ".pass", "")));
        }
        return l;
    }

    /** Сети из камеры (wifi.txt), потом из config.txt — без повторов. Первая — самая приоритетная. */
    static List<Net> networks(Properties cfg) {
        List<Net> out = new ArrayList<Net>();
        try {
            Properties p = new Properties();
            load(p, new File(dir(), "wifi.txt"));
            out.addAll(read(p));
        } catch (IOException e) { /* нет файла */ }
        for (Net n : read(cfg)) if (find(out, n.ssid) < 0) out.add(n);
        return out;
    }

    /** Только сети, добавленные в камере. */
    static List<Net> ownNetworks() {
        Properties p = new Properties();
        try { load(p, new File(dir(), "wifi.txt")); } catch (IOException e) { /* нет файла */ }
        return read(p);
    }

    static int find(List<Net> l, String ssid) {
        for (int i = 0; i < l.size(); i++) if (l.get(i).ssid.equals(ssid)) return i;
        return -1;
    }

    static void saveOwn(List<Net> l) throws IOException {
        Properties p = new Properties();
        for (int i = 0; i < l.size(); i++) {
            p.setProperty("wifi" + (i + 1) + ".ssid", l.get(i).ssid);
            p.setProperty("wifi" + (i + 1) + ".pass", l.get(i).pass);
        }
        FileOutputStream out = new FileOutputStream(new File(dir(), "wifi.txt"));
        try {
            OutputStreamWriter w = new OutputStreamWriter(out, "UTF-8");
            p.store(w, "Wi-Fi networks added on the camera (Proyavka app, Wi-Fi screen)");
            w.flush();
            out.getFD().sync();
        } finally {
            out.close();
        }
    }

    // ---------- Wi-Fi ----------

    static String q(String ssid) { return "\"" + ssid + "\""; }

    static String unq(String ssid) {
        return ssid != null && ssid.length() >= 2 && ssid.startsWith("\"") && ssid.endsWith("\"")
                ? ssid.substring(1, ssid.length() - 1) : ssid;
    }

    static WifiConfiguration configured(WifiManager wifi, String ssid) {
        List<WifiConfiguration> have = wifi.getConfiguredNetworks();
        if (have != null) for (WifiConfiguration h : have) if (q(ssid).equals(h.SSID)) return h;
        return null;
    }

    /** Прописать сеть в Android (добавить или обновить пароль). Возвращает id или -1. */
    static int put(WifiManager wifi, Net n, int priority) {
        WifiConfiguration c = new WifiConfiguration();
        c.SSID = q(n.ssid);
        if (n.pass.length() == 0) {
            c.allowedKeyManagement.set(WifiConfiguration.KeyMgmt.NONE);
        } else {
            c.allowedKeyManagement.set(WifiConfiguration.KeyMgmt.WPA_PSK);
            // 64 hex-символа — готовый ключ, его wpa_supplicant ждёт без кавычек
            c.preSharedKey = n.pass.matches("[0-9a-fA-F]{64}") ? n.pass : q(n.pass);
        }
        c.priority = priority;
        WifiConfiguration h = configured(wifi, n.ssid);
        if (h != null) {
            c.networkId = h.networkId;
            int id = wifi.updateNetwork(c);
            if (id >= 0) return id;
        }
        return wifi.addNetwork(c);
    }

    /**
     * Камера держит список сетей на RAM-диске и восстанавливает его из резервной копии не всегда,
     * поэтому при каждом запуске все сохранённые сети заново прописываются в Android.
     */
    static void applyAll(WifiManager wifi, List<Net> nets) {
        for (int i = 0; i < nets.size(); i++) {
            int id = put(wifi, nets.get(i), 100 - i);
            if (id >= 0) wifi.enableNetwork(id, false);
        }
        wifi.saveConfiguration();
        wifi.reconnect();
    }

    static boolean enableWifi(WifiManager wifi) {
        if (!wifi.isWifiEnabled()) {
            weEnabledWifi = true;
            return wifi.setWifiEnabled(true);
        }
        return true;
    }

    static void restoreWifi(WifiManager wifi) {
        if (weEnabledWifi) {
            wifi.setWifiEnabled(false);
            weEnabledWifi = false;
        }
    }

    static boolean connected(Context ctx) {
        ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
        NetworkInfo ni = cm.getNetworkInfo(ConnectivityManager.TYPE_WIFI);
        return ni != null && ni.isConnected();
    }

    /** Имя сети, к которой подключены сейчас, или null. */
    static String currentSsid(Context ctx, WifiManager wifi) {
        if (!connected(ctx)) return null;
        WifiInfo wi = wifi.getConnectionInfo();
        return wi == null ? null : unq(wi.getSSID());
    }

    // ---------- камера Sony (как в PMCADemo BaseActivity) ----------

    static void notifyAppInfo(android.app.Activity a) {
        Intent i = new Intent("com.android.server.DAConnectionManagerService.AppInfoReceive");
        i.putExtra("package_name", a.getComponentName().getPackageName());
        i.putExtra("class_name", a.getComponentName().getClassName());
        a.sendBroadcast(i);
    }

    /** false — камера не засыпает сама (на время отправки и подключения). */
    static void autoPowerOff(Context ctx, boolean enable) {
        Intent i = new Intent("com.android.server.DAConnectionManagerService.apo");
        i.putExtra("apo_info", enable ? "APO/NORMAL" : "APO/NO");
        ctx.sendBroadcast(i);
    }

    static TextView text(Context ctx, int sp, int color) {
        TextView t = new TextView(ctx);
        t.setTextSize(sp);
        t.setTextColor(color);
        return t;
    }

    static final int GRAY = Color.rgb(150, 150, 150);

    static String plural(int n, String one, String few, String many) {
        int a = n % 10, b = n % 100;
        if (a == 1 && b != 11) return one;
        if (a >= 2 && a <= 4 && (b < 12 || b > 14)) return few;
        return many;
    }

    static String join(List<String> l) {
        StringBuilder sb = new StringBuilder();
        for (String s : l) sb.append(sb.length() > 0 ? ", " : "").append(s);
        return sb.toString();
    }
}
