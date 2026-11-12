package dev.antigravityplus.probe;

import android.app.Activity;
import android.os.Bundle;
import android.widget.ArrayAdapter;
import android.widget.ListView;

public class ListActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        setTitle("Список");
        String[] rows = new String[60];
        for (int i = 0; i < rows.length; i++) rows[i] = "Строка " + i;
        ListView list = new ListView(this);
        list.setPadding(0, 160, 0, 0);
        list.setAdapter(new ArrayAdapter<String>(this, android.R.layout.simple_list_item_1, rows));
        setContentView(list);
    }
}
