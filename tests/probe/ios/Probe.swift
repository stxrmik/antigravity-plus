import SwiftUI
import os

@main
struct ProbeApp: App {
    var body: some Scene { WindowGroup { ContentView() } }
}

struct ContentView: View {
    @State private var name = ""
    @State private var secret = ""
    @State private var count = 0
    @State private var greeting = ""
    var body: some View {
        NavigationStack {
            Form {
                TextField("Имя", text: $name).accessibilityIdentifier("nameField")
                SecureField("Пароль", text: $secret)
                Button("Поздороваться") {
                    greeting = "Привет, \(name)! (пароль: \(secret.count) симв.)"
                    count += 1
                    print("greeted \(name)")
                    Logger(subsystem: "dev.antigravityplus.probe", category: "ui").error("probe error log \(count)")
                }
                Text(greeting).accessibilityIdentifier("greeting")
                Text("Нажатий: \(count)")
                NavigationLink("Дальше") {
                    List(0..<60) { Text("Строка \($0)") }.navigationTitle("Список")
                }
                Button("Упасть") { fatalError("probe crash") }
            }
            .navigationTitle("Проба")
        }
    }
}
