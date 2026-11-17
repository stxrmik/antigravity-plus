package dev.antigravityplus.probe;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.text.InputType;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

public class MainActivity extends Activity {
    private int count = 0;

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(48, 48, 48, 48);
        TextView title = new TextView(this);
        title.setText("Проба");
        title.setTextSize(30);
        final EditText name = new EditText(this);
        name.setId(R.id.nameField);
        name.setHint("Имя");
        name.setSingleLine(true);
        final EditText pass = new EditText(this);
        pass.setId(R.id.passField);
        pass.setHint("Пароль");
        pass.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        final TextView greeting = new TextView(this);
        greeting.setId(R.id.greeting);
        final TextView counter = new TextView(this);
        counter.setId(R.id.counter);
        counter.setText("Нажатий: 0");
        Button greet = new Button(this);
        greet.setText("Поздороваться");
        greet.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                count++;
                greeting.setText("Привет, " + name.getText() + "! (пароль: " + pass.getText().length() + " симв.)");
                counter.setText("Нажатий: " + count);
                Log.i("Probe", "greeted " + name.getText());
                Log.e("Probe", "probe error log " + count);
            }
        });
        Button next = new Button(this);
        next.setText("Дальше");
        next.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { startActivity(new Intent(MainActivity.this, ListActivity.class)); }
        });
        Button crash = new Button(this);
        crash.setText("Упасть");
        crash.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { throw new IllegalStateException("probe crash"); }
        });
        for (View view : new View[] {title, name, pass, greet, greeting, counter, next, crash}) root.addView(view);
        ScrollView scroll = new ScrollView(this);
        scroll.setFitsSystemWindows(true);
        scroll.addView(root);
        setContentView(scroll);
        if (state == null) open(getIntent());
    }

    @Override protected void onNewIntent(Intent intent) { super.onNewIntent(intent); setIntent(intent); open(intent); }

    private void open(Intent intent) {
        if (intent != null && intent.getData() != null && "list".equals(intent.getData().getHost())) {
            intent.setData(null);
            setIntent(intent);
            startActivity(new Intent(this, ListActivity.class));
        }
    }
}
