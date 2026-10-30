package video.airframe;

import android.app.Activity;
import com.google.zxing.integration.android.IntentIntegrator;
import com.google.zxing.integration.android.IntentResult;
import android.content.Intent;
import android.graphics.Color;
import android.media.projection.MediaProjectionManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

public class MainActivity extends Activity {
    private EditText address, code;
    private TextView status;
    private Button start;
    private String pendingAddress, pendingCode;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Runnable refresh = new Runnable() {
        @Override public void run() {
            status.setText(ProjectionService.status);
            start.setEnabled(!ProjectionService.running);
            main.postDelayed(this, 1000);
        }
    };

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        if(saved!=null){pendingAddress=saved.getString("pendingAddress");pendingCode=saved.getString("pendingCode");}
        getWindow().setStatusBarColor(Color.rgb(16,18,22));
        getWindow().setNavigationBarColor(Color.rgb(16,18,22));
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Color.rgb(16,18,22));
        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        int padding = (int)(28 * getResources().getDisplayMetrics().density);
        column.setPadding(padding,padding,padding,padding);
        scroll.addView(column);
        addText(column, "◉  AirFrame", 30, "#B6F5A1");
        addText(column, "Ваш Android. На большом экране.", 22, "#EEF0F3");
        addText(column, "Откройте AirFrame на компьютере. Подключите оба устройства к одной сети Wi-Fi.", 15, "#9299A5");
        Button scan = new Button(this);
        scan.setText(video.airframe.R.string.scan_qr);
        scan.setOnClickListener(v -> {
            if(ProjectionService.running){status.setText("Сначала остановите текущую трансляцию");return;}
            new IntentIntegrator(this).setDesiredBarcodeFormats(IntentIntegrator.QR_CODE)
                    .setPrompt("Наведите камеру на QR в AirFrame на компьютере")
                    .setBeepEnabled(false).setOrientationLocked(false).initiateScan();
        });
        column.addView(scan);
        addText(column, "Или подключитесь вручную", 13, "#9299A5");
        addText(column, "Адрес компьютера", 13, "#B6F5A1");
        address = new EditText(this);
        address.setTextColor(Color.WHITE);
        address.setSingleLine(true);
        address.setHintTextColor(Color.GRAY);
        address.setHint("192.168.1.10:8765");
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        address.setText(getPreferences(MODE_PRIVATE).getString("address", ""));
        column.addView(address);
        addText(column, "Восьмизначный код с компьютера", 13, "#B6F5A1");
        code = new EditText(this);
        code.setTextColor(Color.WHITE);
        code.setHintTextColor(Color.GRAY);
        code.setHint("12345678");
        code.setSingleLine(true);
        code.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_VARIATION_PASSWORD);
        column.addView(code);
        start = new Button(this);
        start.setText("Начать трансляцию");
        start.setTextColor(Color.rgb(16,30,16));
        start.setBackgroundTintList(android.content.res.ColorStateList.valueOf(Color.rgb(182,245,161)));
        start.setOnClickListener(v -> requestCapture());
        column.addView(start);
        Button stop = new Button(this);
        stop.setText("Остановить");
        stop.setOnClickListener(v -> startService(new Intent(this,ProjectionService.class).setAction("STOP")));
        column.addView(stop);
        status = addText(column, ProjectionService.status, 15, "#B6F5A1");
        addText(column, "Разрешите захват экрана в системном диалоге. При блокировке телефона Android может завершить захват — тогда потребуется новое разрешение.", 13, "#9299A5");
        addText(column, "Частота кадров (30–120), битрейт и качество задаются на компьютере. Для высоких fps предпочтительна сеть 5 или 6 ГГц; для 120 fps нужен экран 120 Гц. Звук телефона включается на компьютере (Android 10+); управление телефоном не передаётся.", 13, "#9299A5");
        setContentView(scroll);
    }

    private TextView addText(LinearLayout column, String text, int size, String color) {
        TextView view = new TextView(this);
        view.setText(text); view.setTextSize(size); view.setTextColor(Color.parseColor(color));
        view.setPadding(0,18,0,18); column.addView(view); return view;
    }

    private void requestCapture() {
        if (ProjectionService.running) return;
        String target = address.getText().toString().trim();
        String credential = code.getText().toString().trim();
        if (!target.matches("(?:[0-9]{1,3}\\.){3}[0-9]{1,3}:[0-9]{1,5}")) {
            address.setError("Введите IPv4 и порт с компьютера"); return;
        }
        String[] pieces = target.split(":");
        int port = Integer.parseInt(pieces[1]);
        for(String octet : pieces[0].split("\\.")) {
            if(Integer.parseInt(octet)>255) { address.setError("Некорректный IP"); return; }
        }
        if(port<1 || port>65535) {address.setError("Некорректный порт"); return;}
        if(!credential.matches("[0-9]{8}")) {code.setError("Нужны 8 цифр"); return;}
        beginCapture(target,credential);
    }

    private void beginCapture(String target,String credential) {
        if(ProjectionService.running)return;
        pendingAddress=target; pendingCode=credential;
        MediaProjectionManager manager=(MediaProjectionManager)getSystemService(MEDIA_PROJECTION_SERVICE);
        startActivityForResult(manager.createScreenCaptureIntent(), 100);
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request,result,data);
        IntentResult scanned=IntentIntegrator.parseActivityResult(request,result,data);
        if(scanned!=null){
            if(scanned.getContents()!=null){
                try {
                    Pairing pairing=Pairing.parse(scanned.getContents());
                    address.setText(pairing.address);
                    beginCapture(pairing.address,pairing.key);
                }catch(IllegalArgumentException error){status.setText(error.getMessage());}
            }
            return;
        }
        if(request!=100)return;
        if(result!=RESULT_OK || data==null) {status.setText("Захват экрана не разрешён"); return;}
        if(pendingAddress==null || pendingCode==null) {status.setText("Повторите подключение");return;}
        getPreferences(MODE_PRIVATE).edit().putString("address",pendingAddress).apply();
        Intent service=new Intent(this,ProjectionService.class)
                .putExtra("permission",data).putExtra("resultCode",result)
                .putExtra("address",pendingAddress).putExtra("code",pendingCode);
        startForegroundService(service);
        pendingCode=null; code.setText("");
    }

    @Override protected void onSaveInstanceState(Bundle state){
        state.putString("pendingAddress",pendingAddress);state.putString("pendingCode",pendingCode);
        super.onSaveInstanceState(state);
    }

    @Override protected void onResume() {super.onResume();main.post(refresh);}
    @Override protected void onPause() {main.removeCallbacks(refresh);super.onPause();}
}
