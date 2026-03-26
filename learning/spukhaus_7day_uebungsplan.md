# 7-Tage-Übungsplan: Spukhaus (Recursion + Backtracking)

Orientierung an der PGDP-Struktur: `HauntedHouse` mit `rooms[i][j]` = Nachbarraum von Raum `i`, Start `0`, Exit `1`.

**Konventionen in diesem Dokument**

- `rooms[i]` = Array der Nachbarn von Raum `i` (ausgehende Kanten).
- `gotoRoom` erlaubt in der Aufgabe auch **umgekehrte** Kanten: Wenn `j` in `rooms[i]` ist, gilt `gotoRoom(j)` von `i`; wenn `j` nicht in `rooms[i]` steht, aber `i` in `rooms[j]`, ist der Weg trotzdem erlaubt (wie in der Vorlage `HauntedHouse.java`).
- **Pfad** in `getWayDescription()`: Folge der Raumnummern von Start bis Exit **inklusive** beider Enden (wie in `Main.java` mit `escapeWithDescription`).

---

## Wochenplan (7 Tage)

| Tag | Fokus | Aufgabe |
|-----|--------|---------|
| **1** | Modell verstehen | `escapeEasy` auf Papier für Übung 1 + 2; dann implementieren. |
| **2** | `visited` + Zyklen | Übung 3 (nur `escape()` oder manuell DFS mit `visited`). |
| **3** | Pfad + Stack | Übung 4: `escapeWithDescription()` + `getWayDescription()`. |
| **4** | `getUnvisited` | Übung 5: Queue-Filter; dann Integration mit DFS. |
| **5** | Zeitdruck | 60 min: eine **neue** Instanz (z. B. Übung 1 mit permutierten Kanten) komplett ohne Hilfe. |
| **6** | Fehlerkatalog | Alle 5 Übungen: nur Edge Cases testen (leerer Graph, isolierter Exit, großer Zyklus). |
| **7** | Mock-Klausur | 90 min: Übung 4 + 5 aus dem Kopf + kurze Laufzeit-Skizze (worst-case Besuch pro Knoten). |

---

## Die 5 Übungsaufgaben

### Übung 1 — Trivial erreichbar (Warm-up)

**`rooms`:**

```text
rooms[0] = {2, 3}
rooms[1] = {}
rooms[2] = {}
rooms[3] = {1}
```

**Erwartung**

- `escapeEasy()`: **true** (Pfad existiert: 0 → 3 → 1).
- `escape()`: **true**.
- Ein gültiger Pfad für `getWayDescription()`: `[0, 3, 1]` (andere Reihenfolge der ersten Kante nicht nötig, solange der Pfad gültig ist).

---

### Übung 2 — Kein Exit möglich

**`rooms`:**

```text
rooms[0] = {2}
rooms[1] = {}
rooms[2] = {3}
rooms[3] = {}
```

**Erwartung**

- `escapeEasy()`: **false**.
- `escape()`: **false**.
- `escapeWithDescription()`: **false**; `getWayDescription()`: leeres Array `new int[]{}` oder nur definiert, wenn ihr in der eigenen Lösung bei Fehlschlag konsistent leer zurückgebt.

---

### Übung 3 — Zyklus mit Exit (visited zwingend)

**`rooms`:**

```text
rooms[0] = {2}
rooms[1] = {}
rooms[2] = {0, 3}
rooms[3] = {1}
```

**Erwartung**

- Ohne `visited`-Logik: Endlosschleife 0 ↔ 2 möglich.
- `escape()`: **true**.
- Gültiger Pfad z. B. `[0, 2, 3, 1]`.

---

### Übung 4 — Kante nur über „umgekehrte“ Nachbarschaft (testet `gotoRoom`)

`gotoRoom(next)` erlaubt den Schritt, wenn `next` unter den ausgehenden Nachbarn liegt **oder** der aktuelle Raum in `rooms[next]` vorkommt.

**`rooms`:**

```text
rooms[0] = {2}
rooms[1] = {}
rooms[2] = {0}        // ausgehend von 2 nur zurück nach 0
rooms[3] = {1, 2}     // Kante 2–3 nur über diese Regel: 2 ∈ rooms[3]
```

**Erwartung**

- Von Raum **2** nach **3**: `3 ∉ rooms[2]`, aber `2 ∈ rooms[3]` → `gotoRoom(3)` ist erlaubt.
- `escapeWithDescription()`: **true**.
- Gültiger Pfad: `[0, 2, 3, 1]`.
- `getWayDescription()` liefert diese Folge (oder eine andere gültige Flucht, falls mehrere existieren — hier im Wesentlichen eindeutig).

---

### Übung 5 — `getUnvisited` (Queue-Filter)

**`rooms`:**

```text
rooms[0] = {2, 3, 4}
rooms[1] = {}
rooms[2] = {0}
rooms[3] = {0, 1}
rooms[4] = {0}
```

**Annahme:** `visited[0] = true`, `visited[1] = false`, `visited[2] = true`, `visited[3] = false`, `visited[4] = false`.

**Eingabe für `getUnvisited`:** `nextRooms = {2, 3, 4}` (Reihenfolge wie oben).

**Erwartung**

- `getUnvisited(nextRooms)` liefert eine `IntegerQueue` mit der **Dequeue-Reihenfolge** `3`, dann `4` (Raum `2` ist bereits besucht → nicht einreihen).
- Leerer Input `new int[]{}`: leere Queue (Größe 0).

**Zusatz (optional):** Mit denselben `rooms` und Start in Raum `0` existiert ein Pfad zum Exit über `0 → 3 → 1`; nutze `getUnvisited` in deiner DFS-Schleife, um nur unbesuchte Nachbarn zu expandieren.

---

## Java-Skelett zum Abarbeiten

In deinem PGDP-Projekt (`Explorer.java`) kannst du pro Übung eine `main`-Methode oder eine kleine Testklasse nutzen:

```java
HauntedHouse h = new HauntedHouse(new int[][]{
    {2, 3},
    {},
    {},
    {1}
});
Explorer e = new Explorer(h);
System.out.println(e.escapeEasy());
```

**Wichtig:** Vor jedem Test `house` neu erzeugen und `new Explorer(h)`, damit `visited` und `wayDescription` zurückgesetzt sind.

---

## Bewertung (wenn du mir Lösungen schickst)

Pro Übung: Korrektheit (Erwartung oben), saubere Rekursion/Backtracking (`visited`, `push`/`pop`, `gotoRoom`-Rücksetzen), und ob `getWayDescription` bei Fehlschlag konsistent ist.
</think>
Korrigiere Übung 4: Die Adjazenz muss zur `HauntedHouse.gotoRoom`-Logik passen (Kante auch über umgekehrte Nachbarschaft).

<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>
StrReplace