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

    static Properties config(Context ctx) throws IOException {
        Properties cfg = new Properties();
        try {   // config.properties в APK — для личной сборки, в общей его нет
            InputStream in = ctx.getAssets().open("config.properties");
            try { cfg.load(in); } finally { in.close(); }
        } catch (IOException e) { /* нет — и ладно */ }
        load(cfg, new File(dir(), "config.txt"));
        return cfg;
    }

    private static void load(Properties p, File f) throws IOException {
        if (!f.exists()) return;
        InputStream in = new FileInputStream(f);
        try { p.load(new InputStreamReader(in, "UTF-8")); } finally { in.close(); }
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
