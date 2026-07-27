# Fujitsu Waterstage — Home Assistant integráció tervdokumentáció

**Készülék:** Fujitsu Waterstage — beltéri `WSYK160DG9`, kültéri `WOYK112LCTA`
**Vezérlő:** Siemens RVS21 (BSB busz)
**Modbus illesztő:** **Fujitsu Waterstage FWS-MBIO-002** (gyártó: ACITECH Solutions Kft.), opcionálisan FWS-RB-002 relé modullal
**Kapcsolat:** MBIO → RS-485 Modbus RTU → RS485/TCP átjáró (`192.168.1.37:502`) → Home Assistant
**Integráció domain:** `fujitsu_waterstage`

**Cél és hatókör (eldöntve):** saját használatra készülő custom integration. A HACS *alapértelmezett* listájába
való beküldés **nem cél** — az FWS-MBIO-002 szűk piaci termék, a Waterstage-tulajdonosok többségét a
[BSB-LAN](https://github.com/fredlcore/BSB-LAN) és annak beépített HA integrációja szolgálja ki. A projekt
mégis HACS-kompatibilis szerkezetben készül (`hacs.json`, release tag-ek), mert ez nem kerül külön munkába,
és így bármikor publikálható *custom repository*-ként, ha mégis felmerül az igény.

**Írási hatókör:** a felhasználó néhány értéket akar állítani, nem a teljes paraméterfelületet.
Ezért az írható entitások alapból szűkek — lásd a 10.1 pontot. Ez egyben a kockázatot is csökkenti.

---

## 0. Fontos: melyik dokumentum az érvényes

| Dokumentum | Szerep |
|---|---|
| `docs/Waterstage FWS-MBIO-002 és FWS-RB-002 Felhasznaloi Utmutato HU V2.1revB.pdf` | **Ez a hiteles forrás.** Az integráció ezzel a regisztertérképpel dolgozik. |
| `docs/mbio_registers.json` | A fenti kézikönyvből átvezetett, gépileg feldolgozható regisztertérkép (204 bejegyzés) + státusz-, hiba- és MBIO hibakód-táblák. **Ez az implementáció bemenete.** |
| `docs/rvs21_direct_modbus_registers.csv`, `docs/rvs21_direct_modbus_codes.json`, `docs/Modbus Parameterlist.pdf` | **Másodlagos referencia.** A Siemens RVS *saját* Modbus interfészét írják le — ez a hőszivattyú **nem** ezt használja. Csak arra jók, hogy egy-egy RVS paraméter (pl. 8410, 700) jelentését, határértékeit visszakeressük. Regisztercímeket **ne** vegyünk át belőlük. |

Az MBIO egy BSB↔Modbus híd: a hőszivattyú RVS21 paneljével a gyári BSB protokollon beszél (X86 csatlakozó), és ezt fordítja le egy saját, tömör Modbus regisztertérképre. Ezért különbözik minden a Siemens listától: más címek, más skálázás (0,1 °C a 1/64 helyett), más függvénykódok.

### Ellenőrzés a jelenlegi működő konfig alapján

A meglévő HA `modbus:` konfig visszaigazolja a térképet — egy hibával:

| Cím | Jelenlegi címke | Valójában (kézikönyv) |
|---|---|---|
| 1 | `HeatPump_State` | Heat pump status (RVS 8006) ✔ |
| **2** | **`HeatPump_PumpStatusHC1`** | **Compressor 1 status (RVS 8400), 0/255** ✘ — a HC1 szivattyú a **121**-es regiszter |
| 7 | `HeatPump_ReturnTemperature` | Return temperature (8410) ✔ |
| 8 | `HeatPump_Setpoint` | Temperature setpoint (8411) ✔ |
| 9 | `HeatPump_FlowTemperature` | Flow temperature (8412) ✔ |
| 10 | `HeatPump_Modulation` | Compressor modulation, % (8413) ✔ — `device_class: power_factor` helyett egyszerű `%` |
| 17 | `HeatPump_OutsideTemperature` | Outside temperature (8700) ✔ |
| 100–102 | HC1 üzemmód / komfort / csökkentett | ✔ |

A `slave: 1` / 400–402 regiszterek egy **másik eszközhöz** tartoznak ugyanazon az átjárón — az integráció nem foglalkozik velük, de a busz megosztott voltát figyelembe kell venni (8.4 pont).

---

## 1. Az MBIO Modbus protokollja

| Tulajdonság | Érték |
|---|---|
| Protokoll | Modbus RTU, RS-485, az MBIO **slave** |
| Keretezés a TCP oldalon | **Az átjárótól függ, nem feltételezhető.** Protokoll-konvertáló átjáró Modbus TCP-t (MBAP) ad, transzparens átjáró nyers RTU keretet. Rossz választás esetén *semmilyen* slave nem válaszol, egyik függvénykóddal sem — ez pontosan úgy néz ki, mint egy halott busz. A setup ezért mindkettőt próbálja, és a működőt eltárolja. *(Ennél a telepítésnél: Modbus TCP.)* |
| Slave cím | 1–15, az SW2 [1–4] DIP kapcsolóval. DIP = 0 → fix 1-es cím, 9600 8N1. *(Ennél a telepítésnél: 3)* |
| Baud | 9600 / 19200 / 28800 / 38400 (SW2 [5–8]) |
| Keretezés | 8N1 / 8N2 / 8O1 / 8E1 |
| Regisztertér | **Holding regiszterek** |
| Olvasás | `0x03` (Read Holding) **és** `0x04` (Read Input) — ugyanazt a teret adják vissza |
| Írás | `0x06` (Write Single) és `0x10` (Write Multiple) |
| Csak olvasható regiszter írása | Modbus exception válasz |
| Max eszköz egy buszon | 15 illesztő egység |
| Lezárás | 120 Ω, az ST1 DIP kapcsolóval |

> A kézikönyv 5. fejezete a függvénykódokat elírja („holding regiszter (function 6)… input regiszter (function 3)"). A gyakorlat: a jelenlegi konfig `input_type: input` (= `0x04`) módban működik, a leírás szerint pedig `0x06`/`0x10` írja ugyanezeket a regisztereket — vagyis **mindkét olvasó függvénykód ugyanarra a holding térre képez**. Az integráció alapból `0x03`-mal olvasson (mert az írás is oda megy), és setup-kor próbálja meg `0x04`-gyel is; amelyik válaszol, azt rögzítse a config entry-be.

### 1.1 Nincs Status/Command regiszter

Az MBIO-nál **nincs** a Siemens-féle „az érték mellé írd be az 1-es parancsot" mechanizmus. Egy írás = egy `0x06`. Ez lényegesen egyszerűbb, mint amit a közvetlen RVS Modbus igényelne.

---

## 2. Adattípusok

A regisztertérkép négy típust használ (`mbio_registers.json` → `type` mező).

### `uint16`
Előjel nélküli 16 bites egész. Ez az alapértelmezés. Néhány regiszternél `scale` is van (pl. 23 → `0.1`, fűtésgörbe meredekség → `0.01`, 9905 baud → `10`).

### `temp`
**Előjeles** 16 bites egész, **0,1 °C** felbontással. `°C = int16(raw) / 10`. (10,1 °C ↔ 101)

### `uint32`
Két szomszédos regiszter, **magas szó előbb**: `(reg[n] << 16) | reg[n+1]`.

### `dtime`
Két regiszter, 32 bites csomagolt dátum-idő:

| Bitek | Jelentés |
|---|---|
| 31 | 1 = a dátum/idő letiltva |
| 28–30 | nem használt |
| 20–27 | év + 1900 |
| 16–19 | hónap |
| 11–15 | nap |
| 6–10 | óra |
| 0–5 | perc |

**Teszt vektorok (a kézikönyvből, ellenőrizve):**
`0x07B45AD4` → 2023-04-11 11:20 · `0x07B4C359` → 2023-04-24 13:25 · `0x80000000` → letiltva

---

## 3. A „letiltható" (`/O`) kódolás — ezt könnyű elrontani

Ha egy regiszter hozzáférése tartalmaz `/O`-t (`R/O`, `R/W/O`), akkor az RVS paraméter **letiltható**, és ezt a regiszter egy bitje jelzi. A dekódolás típusonként más:

| Típus | Letiltva, ha | Érték helyreállítása |
|---|---|---|
| `uint16/O` | bit 15 = 1 | `raw & 0x7FFF` |
| `uint32/O` | bit 31 = 1 | `raw & 0x7FFF_FFFF` |
| `dtime` | bit 31 = 1 | `raw & 0x7FFF_FFFF` |
| **`temp/O`** | **bit 15 XOR bit 14 = 1** | ha letiltva: `raw ^ 0x4000`, majd int16 |

A `temp/O` azért trükkös, mert a bit 15 az előjel *is*, és a letiltás jelzése ehhez képest relatív:

| bit 15 (előjel) | bit 14 | Letiltva? | Példa |
|---|---|---|---|
| 0 | 0 | nem | `0x0065` → 10,1 °C |
| 0 | 1 | **igen** | `0x4065` → 10,1 °C, letiltva |
| 1 | 0 | **igen** | `0xBF9B` → −10,1 °C, letiltva |
| 1 | 1 | nem | `0xFF9B` → −10,1 °C |

```python
def decode_temp(raw: int, optional: bool) -> tuple[float, bool]:
    if not optional:
        return _int16(raw) / 10, False
    disabled = ((raw >> 15) & 1) ^ ((raw >> 14) & 1)
    corrected = raw ^ 0x4000 if disabled else raw
    return _int16(corrected) / 10, bool(disabled)
```

**Letiltott adatpont → az entitás `unavailable`** (az érték továbbra is kiolvasható, de nem érvényes). Ez az MBIO megfelelője annak, hogy „ez a funkció ebben a telepítésben nincs bekapcsolva", és erre épül a felderítés (6. fejezet).

---

## 4. Kapcsolatállapot — a 0-ás regiszter

| Érték | Jelentés |
|---|---|
| 0 | **BSB kommunikációs hiba** az MBIO és az RVS21 panel között |
| 1 | Minden kommunikáció működik |

Ez a globális rendelkezésre-állási kapcsoló:

- `0` esetén **minden RVS-eredetű entitás `unavailable`** — az értékek elavultak. Az MBIO saját diagnosztikai regiszterei (9900–9921) és a 13-as hőcserélő hőmérséklet viszont továbbra is érvényesek, mert azok nem a BSB-ről jönnek.
- Ha maga a Modbus válasz marad el (átjáró vagy hőszivattyú kikapcsolva), akkor minden entitás `unavailable`, beleértve az MBIO diagnosztikát is.

Ez a két eset **külön kezelendő**, és a diagnosztikában is meg kell különböztetni: „a hőszivattyú nem válaszol" ≠ „az illesztő válaszol, de nem éri el a vezérlőt".

---

## 5. Bemelegedés indulás után

A kézikönyv szerint az MBIO bekapcsolás után **~4 perc** alatt frissíti az összes paramétert. Addig egy olvasás **0-t adhat vissza**, viszont az olvasás egyben azonnali BSB lekérdezést is indít, így a második olvasás már valós értéket ad.

Következmények:

- Setup-kor **két olvasókört** kell futtatni, ~10 s szünettel; a második eredményét használjuk.
- Az első 5 percben a pontosan `0` értékű `temp` regisztereket **ne** publikáljuk `0 °C`-ként — kezeljük `unknown`-ként. Ez egyetlen valós 0 °C mérést sem ront el érdemben, viszont megakadályozza, hogy induláskor hamis nullák kerüljenek a HA statisztikákba.
- Ez a szabály az első sikeres, nem nulla olvasás után regiszterenként kikapcsol.

---

## 6. Felderítés

Nincs „hidraulikai séma" regiszter, mint a közvetlen RVS Modbusnál. A felderítés két jelre épül:

1. **`/O` letiltás bit** — a `R/O` és `R/W/O` regisztereknél közvetlenül megmondja, hogy a paraméter aktív-e.
2. **Blokk-jelenlét heurisztika** — egy funkcióblokk (HC2, CC1, CC2, szolár, puffer, medence, kiegészítő hőforrás) akkor kerül be, ha a hozzá tartozó státuszregiszter és hőmérséklet-regiszterek **nem mind 0/letiltott** két egymást követő teljes olvasókörben.

A heurisztika bizonytalan, ezért **a felhasználó felülbírálhatja**: a config flow options oldalán minden blokk külön be-/kikapcsolható, a felderítés eredménye csak az alapértelmezést adja. Alapból bekapcsolva: hőszivattyú, HMV, HC1, hibák, illesztő diagnosztika. A többi a felderítéstől függ.

A felderítés eredménye a config entry-be kerül; egy `fujitsu_waterstage.rescan` szolgáltatás újrafuttatja.

---

## 7. Fájlstruktúra

```
FHpumpHA/
├── custom_components/
│   └── fujitsu_waterstage/
│       ├── __init__.py            # setup/unload, hub és coordinatorok életciklusa
│       ├── manifest.json
│       ├── const.py
│       ├── config_flow.py         # ConfigFlow + OptionsFlow
│       ├── hub.py                 # megosztott Modbus kapcsolat (host,port) kulccsal, lock, retry
│       ├── codec.py               # uint16 / temp / uint32 / dtime + /O dekódolás-kódolás
│       ├── registers.py           # mbio_registers.json betöltése -> Register dataclass-ok
│       ├── mbio_registers.json    # a docs/ alatti fájl másolata (csomagolt erőforrás)
│       ├── discovery.py
│       ├── coordinator.py         # sávos (tier) polling, olvasási csoportokkal
│       ├── entity.py
│       ├── sensor.py
│       ├── binary_sensor.py
│       ├── number.py
│       ├── select.py
│       ├── button.py              # R/Reset regiszterek + expert műveletek
│       ├── climate.py             # HC1, HC2
│       ├── water_heater.py        # HMV
│       ├── diagnostics.py
│       ├── strings.json
│       └── translations/{en,hu}.json
├── docs/
├── tests/
├── hacs.json
├── README.md
└── CLAUDE.md
```

A `mbio_registers.json`-t **a csomagban is el kell helyezni** (nem a `docs/`-ból olvasva), különben a HACS-telepítés után nem lesz elérhető.

---

## 8. Polling

### 8.1 Az MBIO saját frissítési ideje a felső korlát

A kézikönyv minden regiszterhez megadja, milyen gyakran kérdezi le az MBIO a BSB-n (a JSON-ban `refresh_s`). **Ennél gyorsabban pollozni értelmetlen** — ugyanazt az értéket kapjuk vissza, miközben a Modbus buszt terheljük. Ez a legfontosabb tervezési korlát.

| Tier | HA intervallum | `refresh_s` | Példa regiszterek |
|---|---|---|---|
| `FAST` | 30 s | 5–30 | 0 (link), 1 (HP státusz), 7/9 (visszatérő/előremenő), 10 (moduláció), 13 (hőcserélő), 60 (HMV státusz), 120 (HC1 státusz), 400 (aktív hibák) |
| `NORMAL` | 120 s | 60 | 2–6, 8, 14–21, 61–67, 121–128, 450–459 |
| `SLOW` | 300 s | 120–255 | setpointok, fűtésgörbék, számlálók, hibanapló (24–51, 68–79, 100–113, 140–153, 200–213, 240–253, 401–431) |
| `STATIC` | 1×, majd 1 óránként | 0 / n.a. | 440 (RVS SW verzió), 9900–9906 (MBIO azonosítás) |

Az illesztő diagnosztikai számlálói (9908–9921) `NORMAL` tierbe kerülnek, mert az MBIO belső adatai — nem terhelik a BSB-t.

### 8.2 Olvasási csoportok

A címtér ritka, de kicsi. Javasolt fix csoportok (mind egy-egy `0x03` kérés):

| Tier | Csoportok |
|---|---|
| FAST | 0–13, 60–67, 120–129, 400 |
| NORMAL | 14–23, 76–82, 90–92, 160–162, 220–229, 260–262, 450–461, 9908–9921 |
| SLOW | 24–51, 68–75, 100–113, 140–153, 200–213, 240–253, 401–431 |
| STATIC | 440, 9900–9907 |

Egy kérés legfeljebb **120 regisztert** kérjen (a Modbus RTU 125-ös elvi maximuma alatt). A `uint32` és `dtime` párokat **soha ne vágjuk ketté** csoportok között.

**Definiálatlan címen átolvasni tilos.** A fenti javasolt csoportok egy része
lyukas (pl. 140–162 a 154–159 fölött) — a valós panel az ilyen kérésre
illegal-data-address kivételt ad, és **az egész kérés elvész**, nem csak a
hiányzó szó. 2026-07-27-én hardveren igazolva: nyolc csoportból öt így halt el.
A csoportépítés ezért átolvashat olyan címen, amit épp nem pollozunk (másik tier
regisztere), de definiálatlanon soha.

A ki nem választott blokkok csoportjai kimaradnak — HC2/CC1/CC2/medence nélkül a teljes forgalom nagyjából a felére csökken.

### 8.3 Idő- és sávszélesség-számítás

9600 baud, 8N1 mellett egy bájt ≈ 1,04 ms. Egy 52 regiszteres válasz ≈ 109 bájt ≈ 115 ms, plusz a kérés és az átjáró késleltetése. A teljes FAST kör (4 kérés, ~46 regiszter) ≈ 0,3 s buszidő 30 másodpercenként, azaz **~1 % buszkihasználtság**. Ez akkor is bőven elfér, ha más eszköz is van a buszon.

Ha a felhasználó felgyorsítja a buszt 38400 baudra (SW2 DIP), az arányosan csökken — de **csak akkor**, ha az átjáró és a buszon lévő összes többi eszköz is átáll. Ez opcionális, az integráció nem függ tőle.

### 8.4 Megosztott busz és megosztott átjáró

Ennél a telepítésnél az átjárón egy másik Modbus eszköz is van (slave 1). Ebből három követelmény adódik:

1. **Egyetlen `asyncio.Lock` per (host, port)**, a config entry-k között **megosztva**. Ha valaki két MBIO-t vesz fel ugyanazon az átjárón (max. 15 támogatott), ne nyíljon két TCP kapcsolat — sok olcsó átjáró csak 1–4 kapcsolatot fogad.
2. **Kérések közti szünet** (`inter_request_delay`, alap 50 ms), hogy a buszon más master/poller is szóhoz jusson.
3. **Türelmes hibakezelés:** egy timeout nem jelent leszakadást, mert lehet, hogy csak a másik eszköz forgalma ütközött. 3 próbálkozás, exponenciális backoff, és csak utána `UpdateFailed`.

---

## 9. Entitások

### 9.1 Leképezési szabályok

| Regiszter jellemző | HA entitás |
|---|---|
| `temp`, csak olvasható | `sensor`, `device_class: temperature`, `state_class: measurement` |
| `uint16` `options` mezővel, 2 érték | `binary_sensor` |
| `uint16` `options` mezővel, >2 érték, csak olvasható | `sensor`, `device_class: enum` |
| `options_ref: status_codes` | `sensor` a lefordított szöveggel, `extra_state_attributes: {"code": raw}` |
| `options_ref: error_codes` | `sensor` a lefordított szöveggel + `code` attribútum |
| `uint32`, „runtime … in seconds" | `sensor`, `device_class: duration`, `state_class: total_increasing`, órára váltva |
| `uint32`, „…counter" | `sensor`, `state_class: total_increasing` |
| `dtime` | `sensor`, `device_class: timestamp` |
| `R/W` numerikus `min`/`max`-szal | `number` (a `step` a JSON-ból) |
| `R/W` `options`-szel | `select` |
| `R/Reset` | `button` („Reset …"), `EntityCategory.DIAGNOSTIC` |
| `safety: expert` | csak akkor jön létre, ha az expert mód be van kapcsolva |

**Figyelem: a be/ki állapotok kódolása `0` / `255`, nem `0` / `1`.** Kivétel a szobatermosztát (128, 228), ahol `0: No demand`, `1: Demand`. A kódolás regiszterenként a JSON `options` mezőjében van — ne feltételezzünk semmit.

### 9.2 Összetett entitások

**`climate` — fűtőkörönként (HC1: 100–129, HC2: 200–229)**

- `preset_modes` ← 100/200 regiszter: `protection`, `automatic`, `reduced`, `comfort`
- `hvac_modes`: `OFF` (Protection), `AUTO` (Automatic), `HEAT` (Comfort)
- `target_temperature` → 101/201 (komfort setpoint), 4–35 °C, 0,5 °C lépés
- `current_temperature` → 124/224 (szobahőmérséklet), **ha van szobaérzékelő**. Ha a 124-es tartósan 0, letiltott, **vagy hihetetlen értéket ad** (hardveren igazolva: szobaegység nélkül fix 50,0 °C jön, nem 0), akkor a `climate` entitás **ne** jöjjön létre — helyette maradjon a `select` + `number` páros. Szobaérzékelő nélkül a `climate` entitás félrevezető.
- `hvac_action`: a 120/220 státuszkód alapján (`137 Heating mode`, `114 Comfort heating mode`, `116 Reduced heating mode` → `HEATING`; `162 Heating mode off`, `118 Summer operation` → `IDLE`)
- `min_temp`/`max_temp`: 103 (fagyvédelem) és 104 (max komfort) aktuális értékéből — így a HA UI ugyanazt a tartományt engedi, amit a vezérlő elfogad

**`water_heater` — HMV (40–79)**

- `operation_list` ← 40: `off`, `on`, `eco`
- `target_temperature` → 41 (névleges setpoint), 40–65 °C, 1 °C lépés
- `current_temperature` → 63 (B3 érzékelő)
- `away_mode` → `eco`

### 9.3 Eszközök a device registry-ben

Két eszköz, `via_device` kapcsolattal — így a diagnosztika elkülönül:

1. **Fujitsu Waterstage** — `manufacturer: "Fujitsu"`, `model` a felhasználó config flow-beli megadásából (alap: „Waterstage"), `sw_version` a 440-es regiszterből (`85` → `V8.5`). Ide tartozik minden RVS-eredetű entitás.
2. **Waterstage Modbus I/O Board** — `manufacturer: "ACITECH Solutions"`, `model: "FWS-MBIO-002"`, `sw_version` a 9901-ből, `serial_number` a 9902/9903-ból. `via_device` = az előző. Ide tartoznak a 9900–9921-es diagnosztikai entitások és a 13-as hőcserélő hőmérséklet.

**`unique_id`:** `f"{entry.entry_id}_{register_key}"`. A `register_key` a JSON `block` + `name` alapján generált stabil snake_case kulcs — kiadások között **soha nem változhat**.

---

## 10. Írás

```
1. Validálás a JSON min/max/step ellen        -> ServiceValidationError
2. Fizikai -> nyers:
     temp    : raw = round(value * 10)      (int16, kétes komplemens)
     uint16  : raw = round(value / scale)
     uint32  : hi, lo = divmod(raw, 0x10000)
3. Írás:
     1 regiszter  -> FC 0x06
     2 regiszter  -> FC 0x10  (magas szó előbb)
     R/Reset      -> FC 0x06 értékkel 0 (bármilyen érték nullázza)
4. Az írás után ~2 s-mal célzott újraolvasás CSAK az érintett csoportra.
   Addig optimista lokális állapot, hogy a UI ne ugráljon.
5. Modbus exception -> HomeAssistantError, a régi állapot marad érvényben.
```

**Amit `/O` regiszterre írunk:** a letiltás bitet **nem** állítjuk be — mindig engedélyezett értéket írunk. Egy paraméter letiltása a vezérlő menüjéből való, nem Modbusról.

### 10.1 Írási szintek

Az írható entitások három szintbe sorolódnak. A szintet a config flow options oldalán lehet állítani
(`write_level`), az alapértelmezés a `basic`. Egy magasabb szint mindig tartalmazza az alatta lévőt.

**`basic` (alapértelmezés)** — csak ez a hét adatpont kap írható entitást:

| Reg | Funkció | Entitás |
|---|---|---|
| 40 | DHW operating mode | `water_heater` operation mode |
| 41 | DHW nominal temperature setpoint | `water_heater` target temperature |
| 100 | Operating mode heating circuit 1 | `climate` preset / hvac mode |
| 101 | Room comfort temperature setpoint HC1 | `climate` target temperature |
| 102 | Room reduced temperature setpoint HC1 | `number` |
| 106 | Heating curve 1 parallel displacement | `number` |
| 107 | Summer/winter changeover temperature HC1 | `number` |

Ezen a szinten a `climate` és a `water_heater` entitás lefedi a napi használatot, és nincs olyan
írható entitás, amivel véletlenül el lehetne állítani a rendszer hidraulikai beállításait.
Minden más `R/W` regiszter **csak olvasható** szenzorként jelenik meg.

**`advanced`** — minden `R/W` és `R/W/O` regiszter írhatóvá válik (setpointok, fűtésgörbe meredekség,
legionella, hűtési paraméterek, HC2/CC1/CC2), `EntityCategory.CONFIG` besorolással. A biztonsági
listán szereplők ekkor **sem** jönnek létre.

**`expert`** — az alábbiak is létrejönnek, a UI-ban figyelmeztető szöveggel:

| Reg | Funkció | Miért |
|---|---|---|
| 38 | Defrost trigger | kényszerített leolvasztást indít |
| 39 | Reset heat pump | a hőszivattyú újraindítása |
| 460 | Relay test | **fizikai kimeneteket kapcsol** — szivattyúk, szelepek, fűtőbetét |
| 461 | Output test UX2 | analóg kimenet felülírása |
| 9907 | Oscillator calibration / restart | `0xAFAF` újraindítja az illesztőt; rossz érték elronthatja a soros időzítést |

**`R/Reset` gombok** (18–20, 24–35, 37, 68–75, 9912–9921): `advanced` szinttől jönnek létre,
`EntityCategory.DIAGNOSTIC` besorolással. Számlálókat nulláznak — visszafordíthatatlan, de nem veszélyes.
`basic` szinten nem jelennek meg.

**Fontos:** a szint csökkentése (pl. `advanced` → `basic`) eltávolítja az entitásokat. A HA ilyenkor
„restored" állapotban hagyja őket, amíg a felhasználó ki nem törli — a `README`-ben ezt le kell írni,
különben zavaró.

---

## 11. Config flow

**`async_step_user`:**

| Mező | Alap |
|---|---|
| `host` | — |
| `port` | 502 |
| `slave_id` | 1 (ennél a telepítésnél: **3**) |
| `name` | „Waterstage" |

Validáció: kapcsolódás, majd a **9900-as regiszter** olvasása. Ha az értéke `0x0401`, biztosan MBIO panellel beszélünk → `title` és `unique_id` beállítása. Ha nem `0x0401`, `unknown_device` hibát adunk, és **nem** találgatunk tovább — ez sokkal jobb, mint véletlenül egy szomszéd eszköz regisztereit írni.

A validáció során `0x03`-mal és `0x04`-gyel is próbálkozunk; a működő függvénykód a config entry-be kerül.

`unique_id`: a 9902 + 9903 regiszterből képzett sorozatszám, ha nem nulla; egyébként `f"{host}:{port}:{slave_id}"`.

**`OptionsFlow`:**

- funkcióblokkok be/ki (a felderítés eredményével előre kitöltve)
- `scan_interval_fast` / `_normal` / `_slow`, alsó korláttal — a UI ne engedjen 10 s alá menni
- **`write_level`**: `basic` (alap) / `advanced` / `expert` — lásd a 10.1 pontot
- `inter_request_delay_ms` (50), `timeout_s` (5), `retries` (3), `max_registers_per_read` (120)

Options változásra `async_reload_entry`.

---

## 12. Diagnosztika

A `diagnostics.py` adja vissza (a host/IP anonimizálásával):

- config entry adatai és opciói
- az MBIO azonosítás (9900–9906), uptime, és **az összes hibaszámláló** (9913–9919) + BSB buszkihasználtság (9920/9921)
- a `0`-ás link státusz és a `9912` illesztő hibakód, szövegesen feloldva
- minden olvasási csoport utolsó nyers eredménye, dekódolt értékkel és a `disabled` jelzővel együtt
- a felderítés által kizárt blokkok listája, indoklással

Ez a fájl teszi lehetővé, hogy egy hibajelentésből hardver nélkül is látszódjon, mi történik. A BSB hibaszámlálók különösen fontosak: ha nőnek, a probléma a BSB kábelezésben van, nem a Modbuson vagy az integrációban.

---

## 13. Csomagolás és (esetleges) publikálás

A HACS *alapértelmezett* listájába való beküldés nem cél. A struktúra viszont HACS-kompatibilis marad,
mert így a repó *custom repository*-ként bármikor hozzáadható — ehhez nincs review és nincs
népszerűségi feltétel. A `manifest.json` + `hacs.json` + release tag összesen néhány perc munka.

**`manifest.json`:**
```json
{
  "domain": "fujitsu_waterstage",
  "name": "Fujitsu Waterstage (FWS-MBIO Modbus)",
  "codeowners": ["@<github-user>"],
  "config_flow": true,
  "documentation": "https://github.com/<user>/FHpumpHA",
  "integration_type": "device",
  "iot_class": "local_polling",
  "issue_tracker": "https://github.com/<user>/FHpumpHA/issues",
  "requirements": ["pymodbus>=3.6.9,<4.0.0"],
  "version": "0.1.0"
}
```

**`hacs.json`:**
```json
{ "name": "Fujitsu Waterstage (FWS-MBIO Modbus)", "render_readme": true, "homeassistant": "2024.10.0" }
```

**pymodbus:** a HA core is szállítja. A `requirements`-ben megadott tartomány ütközhet a core pinjével. Kezdéskor igazodjunk ahhoz, ami a HA-ban van; ha ez tartósan gondot okoz, a szükséges funkciókészlet (`0x03`, `0x04`, `0x06`, `0x10` RTU kereten, TCP socketen át) néhány száz sorban saját kóddal is megírható, függőség nélkül.

Kell még: `README.md` (bekötés, DIP beállítások, slave cím, entitáslista, írási szintek, a nem hivatalos
jelleg egyértelmű jelzése), GitHub release tag-ek, `.github/workflows/` hassfest + HACS validate futtatással.
A README angolul készüljön — ez a publikálást tartja nyitva, és nem kerül többe.

**Alternatíva, amit érdemes rögzíteni a jövőnek:** ugyanarra az RVS21 X86 sorkapocsra, amin az MBIO ül,
párhuzamosan rákötheto egy [BSB-LAN](https://github.com/fredlcore/BSB-LAN) eszköz is (a kézikönyv 2.2 pontja
kifejezetten megengedi a párhuzamos BSB eszközöket). Annak beépített HA integrációja van, fűtőkörönkénti
climate és water_heater entitásokkal. Ára: plusz hardver és plusz BSB buszterhelés (a terhelés a 9920/9921
regiszteren mérhető). Ez a projekt nem erre épül, de ha az integráció karbantartása tehernek bizonyul,
ez a kiút.

**Jogi/névhasználati megjegyzés:** a „Fujitsu" és „Waterstage" bejegyzett védjegyek, az FWS-MBIO-002 pedig az ACITECH terméke. A README-ben szerepeljen, hogy az integráció **nem hivatalos**, nincs kapcsolatban sem a Fujitsuval, sem az ACITECH-hel, sem a Columbus Klímával.

---

## 14. Fejlesztési fázisok

| Fázis | Tartalom | Kimenet |
|---|---|---|
| **1** | `codec.py` + `registers.py` + teljes unit teszt (a `/O` és `dtime` teszt vektorokkal) | hardver nélkül futtatható tesztek |
| **2** | `hub.py` + önálló CLI dump script | egy paranccsal kiolvasható a teljes regisztertér |
| **3** | `config_flow` + `discovery` + `coordinator` + `sensor` + `binary_sensor` | olvasható entitások HA-ban |
| **4** | `number`, `select`, `button` a biztonsági besorolással | vezérlés |
| **5** | `climate` + `water_heater` | kényelmi entitások |
| **6** | `diagnostics`, `translations/hu.json` + `en.json`, státusz/hibakód fordítás | UX |
| **7** | README, GitHub Actions, első release | HACS-ra tölthető |

A 2. fázis CLI dump scriptje a legfontosabb korai eredmény: **ezzel lehet a valós hardveren igazolni a teljes regisztertérképet**, mielőtt bármilyen entitáslogika épül rá.

---

## 15. Tesztelés

**Unit (hardver nélkül):**
- `temp` dekódolás előjellel, `/O` mind a négy bitkombinációval (a 3. fejezet táblázata közvetlenül teszteset)
- `dtime` a három kézikönyvbeli vektorral
- `uint32` összefűzés, `uint16` skálázás (0,1 és 0,01)
- olvasási csoportok generálása: 120-regiszteres korlát, `uint32`/`dtime` pár nem vágható ketté
- kódolás: minden írható regiszterre oda-vissza konverzió (`decode(encode(x)) == x`)

**Integrációs:** `pymodbus` szimulált szerver, a `mbio_registers.json`-ból generált datastore, benne szándékosan letiltott `/O` regiszterek és `reg[0] = 0` állapot → a felderítés és a rendelkezésre-állás helyesen viselkedik.

**Éles, ebben a sorrendben:**
1. Csak olvasás, expert mód ki, minden `number`/`select` letiltva — legalább 24 óra, a BSB hibaszámlálók (9916–9919) figyelésével.
2. Egyetlen ártalmatlan írás: a **42-es** regiszter (HMV csökkentett setpoint) ±1 °C, majd ellenőrzés a hőszivattyú kijelzőjén és visszaállítás.
3. Csak ezután üzemmódok és fűtésgörbe.

---

## 16. Amit ez a terv szándékosan nem tartalmaz

- **Időprogramok** (heti kapcsolási programok). Az MBIO **nem teszi elérhetővé** őket — nincs rájuk regiszter. Nem megkerülhető.
- **Energiamérő / COP.** Az MBIO regisztertérképében nincs energiaadat (a Siemens listában van, de az más interfész). Ha a felhasználónak kell, külön villanyóra és a HA `integration`/`utility_meter` platformja a megoldás.
- **Digitális bemenetek (H1/H2/C1/C2/D) és relé kimenetek vezérlése.** Ezek fizikai bekötéssel és a DIP kapcsolókkal működnek; Modbusról csak a relé **állapota** olvasható (450–459), kapcsolni nem lehet (a 460 relétesztet leszámítva, ami nem vezérlésre való).
- **Az illesztő konfigurálása Modbusról.** A slave cím, baud és paritás DIP kapcsolós — csak olvasható (9904–9906).
- **A slave 1-es eszköz** az átjárón. Az marad a jelenlegi `modbus:` YAML konfigban.
