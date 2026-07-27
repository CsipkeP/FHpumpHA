# CLAUDE.md — FHpumpHA

Home Assistant custom integration a Fujitsu Waterstage hőszivattyúhoz
(beltéri `WSYK160DG9`, kültéri `WOYK112LCTA`), **Fujitsu Waterstage FWS-MBIO-002**
Modbus I/O illesztő panelen keresztül, RS485↔TCP átjáróval.

Domain: `fujitsu_waterstage` — a kód a `custom_components/fujitsu_waterstage/` alatt.

## Olvasd el először

1. **`docs/DESIGN.md`** — a teljes specifikáció. Ez a mérvadó.
2. **`docs/mbio_registers.json`** — a regisztertérkép gépi formában (204 bejegyzés) + státusz-,
   hiba- és MBIO hibakód-táblák. **Ez az implementáció bemenete.**
3. `docs/Waterstage FWS-MBIO-002 ... V2.1revB.pdf` — az eredeti kézikönyv. Vitás esetben ez dönt.

## FIGYELEM: melyik dokumentum NEM érvényes

A `docs/Modbus Parameterlist.pdf`, `docs/rvs21_direct_modbus_registers.csv` és
`docs/rvs21_direct_modbus_codes.json` a Siemens RVS **saját** Modbus interfészét írja le.
**Ez a hőszivattyú nem azt használja.** Regisztercímet, skálázást, függvénykódot ezekből
átvenni hiba. Csak arra jók, hogy egy RVS paraméterszám (pl. 8410, 700) jelentését
visszakeressük — a `mbio_registers.json` `rvs_param` mezője erre hivatkozik.

## Nem alkudható protokoll-szabályok

- **Holding regiszterek.** Olvasás `0x03` (setup-kor `0x04`-et is próbálni kell, mindkettő
  ugyanarra a térre képez), írás `0x06` / `0x10`.
- **`temp` típus: előjeles int16, 0,1 °C** — `°C = int16(raw) / 10`. NEM 1/64.
- **`uint32` és `dtime`: 2 regiszter, magas szó előbb.** Ezt a párt olvasási csoportok
  között soha nem szabad kettévágni.
- **`/O` letiltás dekódolása** (`R/O`, `R/W/O` hozzáférésnél):
  - `uint16` → bit15 = 1 jelenti a letiltást, érték `raw & 0x7FFF`
  - `uint32` / `dtime` → bit31 = 1, érték `raw & 0x7FFFFFFF`
  - **`temp` → `disabled = bit15 XOR bit14`; ha letiltva, az érték `raw ^ 0x4000`, majd int16.**
    Ez a legkönnyebben elrontható rész, a DESIGN.md 3. fejezetében táblázat és teszt vektorok vannak hozzá.
- **Letiltott adatpont → az entitás `unavailable`**, nem 0.
- **A 0-ás regiszter a link státusz.** `0` = BSB hiba az illesztő és az RVS21 között → minden
  RVS-eredetű entitás `unavailable`, de a 13-as és a 9900–9921-es entitások érvényesek maradnak.
- **A be/ki állapotok kódolása `0` / `255`**, kivéve a szobatermosztátot (128, 228: `0`/`1`).
  Mindig a JSON `options` mezőjét kell nézni, soha nem feltételezni.
- **Ne pollozz gyorsabban, mint a JSON `refresh_s` értéke** — az illesztő úgysem frissíti
  gyakrabban a BSB-ről, csak a buszt terheled.
- **Egyetlen `asyncio.Lock` per (host, port), config entry-k között megosztva.** Az átjárón
  más eszköz is lehet, és sok átjáró csak 1–4 TCP kapcsolatot fogad.
- **Nincs Status/Command regiszter.** Egy írás = egy `0x06`. (Ez a Siemens séma sajátja volt.)

## Írási szintek — alapból SZŰK a felület

A `write_level` opció három értéket vehet fel, az alapértelmezés **`basic`**.

- **`basic`** — csak ez a hét regiszter írható: `40`, `41` (HMV üzemmód és setpoint), `100`, `101`
  (HC1 üzemmód és komfort setpoint), `102`, `106`, `107`. Ezeket a `climate` és `water_heater`
  entitás, plusz három `number` fedi le. **Minden más `R/W` regiszter csak olvasható szenzor.**
- **`advanced`** — minden `R/W` és `R/W/O` írható (`EntityCategory.CONFIG`), plusz az `R/Reset` gombok.
- **`expert`** — ezen felül: `38` Defrost trigger · `39` Reset heat pump · `460` Relay test
  (**fizikai kimeneteket kapcsol**) · `461` Output test UX2 · `9907` soft restart (`0xAFAF`).

A `basic` az alapértelmezés, mert a felhasználó kifejezetten csak néhány értéket akar állítani.
Ne hozz létre írható entitást azért, mert a regiszter `R/W` — a szintet kell nézni.

## Publikálás

A HACS alapértelmezett listájába **nem** megyünk be. A struktúra maradjon HACS-kompatibilis
(`hacs.json`, `manifest.json`, release tag), hogy custom repository-ként bármikor megosztható legyen.
A README angolul készüljön.

## Kódolási elvek

- Python 3.12+, teljes type hint, `async` mindenütt. Blokkoló I/O tilos az event loopban.
- HA modern minták: `DataUpdateCoordinator`, `ConfigEntry`, `EntityDescription`, `_attr_` attribútumok.
- A `mbio_registers.json` **másolata a csomagba is kell** (`custom_components/fujitsu_waterstage/`),
  különben HACS-telepítés után nem érhető el.
- `unique_id` = `f"{entry.entry_id}_{register_key}"`. A `register_key` kiadások között soha nem változhat.
- `strings.json` + `translations/en.json` + `translations/hu.json`. A README angol.
- Minden `codec.py` függvényhez unit teszt. A DESIGN.md 3. és 2. fejezetének táblázatai
  közvetlenül tesztesetek — vedd át őket szó szerint.

## Fejlesztési sorrend

A `docs/DESIGN.md` 14. fejezete szerint. Ne ugorj előre: a `codec.py` és a `registers.py`
tesztekkel együtt készüljön el, mielőtt bármilyen entitáskód elkezdődik. A 2. fázis CLI dump
scriptje az első dolog, ami valós hardveren igazolja a regisztertérképet.

## Ismert tények erről a konkrét telepítésről

- **Az átjáró Modbus TCP-t (MBAP) beszél, NEM nyers RTU-t.** A működő YAML
  `type: tcp` — nem `rtuovertcp`. 2026-07-27-én a hardveren igazolva: RTU
  keretezéssel mind a 14 slave néma, mindkét függvénykóddal. A `hub.py` ezért
  keretezést is felderít (`tcp` / `rtu`), nem feltételezi.
- **A panel elutasítja azt az olvasást, ami definiálatlan címet is érint** —
  és nem csak a hiányzó szót veszíted el, hanem a *teljes* kérést. 2026-07-27-én
  igazolva: a 8 csoportból 5 elhalt egyetlen lyuk miatt. Olvasási csoport tehát
  átolvashat olyan címen, amit épp nem pollozunk, de definiálatlanon soha.
- **Szobaérzékelő nélkül a vezérlő 50,0 °C-ot ad a 124/224-en, nem 0-t**, a be
  nem kötött előremenő alapjelre (262) pedig 140,0 °C-ot. A „nem nulla → létezik"
  szabály ezért kevés: ahol van státuszregiszter, az dönt.
- **Ehhez a telepítéshez nincs HMV bekötve.** A DESIGN eredetileg mindig
  bekapcsolt blokknak vette — nem az. A 60-as státusz `---`, miközben a 40/41
  setpoint konfigurált értéket tart.
- Élő blokkok: hőszivattyú, HC1, **CC1** (a 160-as valódi státuszkódot ad),
  hibák, relék, illesztő. HC2/CC2/szolár/puffer/medence/HMV nincs.
- Átjáró: `192.168.1.37:502`, MBIO slave ID = **3**. Ugyanazon az átjárón egy másik eszköz is
  van slave 1-en — az nem tartozik ide, marad a meglévő `modbus:` YAML konfigban.
- A jelenlegi működő YAML `input_type: input` (FC `0x04`) módban olvas, tehát a `0x04` biztosan működik.
- A jelenlegi YAML-ben a **2-es regiszter „PumpStatusHC1" néven szerepel, ez téves** —
  a 2-es a Compressor 1 status (0/255). A HC1 szivattyú a **121**-es regiszter.
