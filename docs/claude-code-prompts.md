# Promptok a Claude Code-hoz

A fázisok a `docs/DESIGN.md` 14. fejezetét követik. **Egyszerre egy fázist adj oda** — a
teljes integrációt egy prompttal kérni megbízhatatlan, és a hibák egymásra rakódnak.
Minden fázis végén futtasd le a teszteket, mielőtt a következőt indítod.

---

## 1. prompt — alapok (1. és 2. fázis)

> Ez az első és legfontosabb lépés. Ne siess vele: minden későbbi fázis erre épül.

```text
Home Assistant custom integrationt építünk a Fujitsu Waterstage hőszivattyúmhoz, egy
FWS-MBIO-002 Modbus illesztő panelen keresztül. A domain: fujitsu_waterstage.

Olvasd el ELŐSZÖR, ebben a sorrendben:
  1. CLAUDE.md
  2. docs/DESIGN.md  — ez a mérvadó specifikáció
  3. docs/mbio_registers.json — a regisztertérkép, ez az implementáció bemenete

FIGYELEM: a docs/Modbus Parameterlist.pdf és a docs/rvs21_direct_modbus_* fájlok egy MÁSIK
interfészt írnak le, amit ez a hőszivattyú NEM használ. Regisztercímet, skálázást vagy
függvénykódot ezekből átvenni hiba.

Ebben a menetben CSAK a DESIGN.md 1. és 2. fázisát csináld meg:

1. custom_components/fujitsu_waterstage/codec.py
   Az mbio_registers.json négy típusának dekódolása és kódolása:
   uint16, temp (előjeles int16, 0.1 °C), uint32 (2 regiszter, magas szó előbb), dtime.
   Plusz a "/O" letiltás-bit kezelése, ami típusonként MÁS — a DESIGN.md 3. fejezete
   részletezi. A temp/O szabálya: disabled = bit15 XOR bit14, és ha letiltva, akkor az
   értékhez raw ^ 0x4000 kell, mielőtt int16-ként értelmezed.
   Minden dekódoló adja vissza az értéket ÉS a disabled jelzőt.

2. custom_components/fujitsu_waterstage/registers.py
   Az mbio_registers.json betöltése fagyasztott dataclass-okba. Generálj minden
   regiszterhez stabil snake_case kulcsot a block + name mezőből — ez lesz a unique_id
   alapja, és kiadások között soha nem változhat, úgyhogy determinisztikus legyen.
   Másold be a JSON-t a custom_components/fujitsu_waterstage/ alá is (a csomagnak
   önállóan működnie kell, a docs/ nélkül).

3. custom_components/fujitsu_waterstage/hub.py
   pymodbus alapú aszinkron kliens Modbus RTU over TCP átjáróhoz.
   Kötelező: egyetlen asyncio.Lock (host, port) kulccsal, config entryk között megosztva;
   kérések közti szünet; timeout; 3 próbálkozás exponenciális backoffal; olvasás 0x03-mal,
   írás 0x06 / 0x10. Az olvasási csoportok generálása is ide jön: max 120 regiszter
   kérésenként, és uint32/dtime párt SOHA nem szabad két kérés közt kettévágni.

4. tools/dump.py
   Önálló CLI script (nem HA-függő): host, port, slave id paraméterekkel kiolvassa a teljes
   regisztertérképet, és táblázatosan kiírja a nyers értéket, a dekódolt értéket, a
   disabled jelzőt és a regiszter nevét. Ez lesz az első dolog, amit valós hardveren futtatok.

5. tests/ — pytest, hardver nélkül futtatható.

Kötelező tesztesetek, ezeket szó szerint vedd át:
  temp/O mind a négy bitkombinációja:
    0x0065 ->  10.1 °C, nincs letiltva
    0x4065 ->  10.1 °C, LETILTVA
    0xBF9B -> -10.1 °C, LETILTVA
    0xFF9B -> -10.1 °C, nincs letiltva
  dtime:
    0x07B45AD4 -> 2023-04-11 11:20
    0x07B4C359 -> 2023-04-24 13:25
    0x80000000 -> letiltva
  Továbbá: uint16 skálázás (0.1 és 0.01), uint32 összefűzés, oda-vissza konverzió
  (decode(encode(x)) == x) minden írható regiszterre, és az olvasási csoportok
  generálása (120-as korlát betartva, uint32/dtime pár nem vágható ketté).

Amit ebben a menetben NE csinálj:
  - semmilyen HA entitást, config flow-t, coordinatort
  - ne találgass regisztercímeket: ami nincs a JSON-ban, az nem létezik

Fontos: nincs hozzáférésed a hardverhez, és a hőszivattyú lehet, hogy ki van kapcsolva.
Minden tesztnek hardver nélkül kell futnia. Ha valami a specifikációból nem egyértelmű,
kérdezz, ne találgass.

Zárásként futtasd le a teszteket, és foglald össze, mit ellenőriztél.
```

---

## 2. prompt — kapcsolat és szenzorok (3. fázis)

```text
Folytatjuk a fujitsu_waterstage integrációt. Olvasd el a CLAUDE.md-t és a docs/DESIGN.md-t,
majd nézd meg, mi készült el a codec.py / registers.py / hub.py fájlokban.

Most a DESIGN.md 3. fázisa jön: config_flow.py, discovery.py, coordinator.py, entity.py,
sensor.py, binary_sensor.py.

Kulcspontok, amikre külön figyelj:
  - A config flow validációja a 9900-as regisztert olvassa. Ha nem 0x0401, unknown_device
    hibát adj, és NE próbálkozz tovább — az átjárón más eszköz is van, azt nem szabad
    piszkálni. Setup közben 0x03 és 0x04 olvasást is próbálj, a működőt tárold el.
  - Négy külön DataUpdateCoordinator a négy tierhez (FAST/NORMAL/SLOW/STATIC), mind
    ugyanazt a hubot használja. A tiereket és az olvasási csoportokat a DESIGN.md 8. fejezete
    adja meg — ne pollozz gyorsabban, mint a JSON refresh_s értéke.
  - A 0-ás regiszter a link státusz. Ha 0, minden RVS-eredetű entitás unavailable, DE a
    13-as és a 9900–9921-es entitások érvényesek maradnak. Ez két külön hibaeset, mint
    amikor a Modbus válasz teljesen elmarad — a diagnosztikában is különüljön el.
  - Letiltott (/O) adatpont -> az entitás unavailable, NEM 0.
  - A be/ki állapotok 0/255 kódolásúak, kivéve a szobatermosztátot (128, 228: 0/1).
    Mindig a JSON options mezőjét nézd.
  - Indulás után ~4 percig a 0 érték lehet "még nem tudom" — a DESIGN.md 5. fejezete írja
    le, hogyan kezeld.
  - Két eszköz a device registryben (hőszivattyú + MBIO panel), via_device kapcsolattal.

A státusz- és hibakódok szöveges feloldása az mbio_registers.json status_codes /
error_codes / mbio_error_codes tábláiból jöjjön, a nyers kód extra_state_attributes-ban.

Írható entitást MÉG NE csinálj, az a következő fázis.
Bővítsd a teszteket, és a végén futtasd le mindet.
```

---

## 3. prompt — vezérlés (4. és 5. fázis)

```text
Folytatjuk a fujitsu_waterstage integrációt. Olvasd el a CLAUDE.md-t és a docs/DESIGN.md-t,
és nézd meg a meglévő kódot.

Most a 4. és 5. fázis: number.py, select.py, button.py, climate.py, water_heater.py.

A LEGFONTOSABB: az írási szintek. A write_level opció alapértelmezése "basic", és ezen a
szinten PONTOSAN hét regiszter írható: 40, 41, 100, 101, 102, 106, 107. Minden más R/W
regiszter csak olvasható szenzor marad. Ne hozz létre írható entitást azért, mert a
regiszter R/W — a DESIGN.md 10.1 pontjában lévő szintet kell nézni.
Az "expert" szint alatti regisztereket (38, 39, 460, 461, 9907) alapból létre se hozd.

Írási útvonal: validálás a JSON min/max/step ellen, majd 0x06 (1 regiszter) vagy 0x10
(2 regiszter). Írás után ~2 másodperccel célzott újraolvasás CSAK az érintett csoportra,
addig optimista lokális állapot. /O regiszterre mindig engedélyezett értéket írj, a
letiltás bitet soha ne állítsd be.

A climate entitás HC1-re: ha a 124-es (szobahőmérséklet) tartósan 0 vagy letiltott, akkor
NE hozd létre a climate entitást — szobaérzékelő nélkül félrevezető. Helyette maradjon a
select + number páros. A DESIGN.md 9.2 pontja részletezi.

Bővítsd a teszteket az írási úttal és a szintek szerinti entitás-létrehozással.
```

---

## 4. prompt — befejezés (6. és 7. fázis)

```text
Utolsó menet a fujitsu_waterstage integrációhoz. Olvasd el a CLAUDE.md-t és a
docs/DESIGN.md-t, majd készítsd el:

  - diagnostics.py a DESIGN.md 12. fejezete szerint (host anonimizálva, MBIO azonosítás,
    az összes hibaszámláló és BSB buszkihasználtság, a nyers + dekódolt csoportértékek,
    és a felderítés által kizárt blokkok indoklással)
  - strings.json + translations/en.json + translations/hu.json
  - manifest.json, hacs.json
  - README.md ANGOLUL: bekötés, DIP kapcsoló beállítások, slave cím, entitáslista,
    az írási szintek magyarázata, és egyértelműen jelezve, hogy ez nem hivatalos
    integráció, nincs kapcsolata a Fujitsuval, az ACITECH-hel vagy a Columbus Klímával
  - .github/workflows/ hassfest és HACS validate futtatással

A HACS alapértelmezett listájába nem megyünk be, de a struktúra legyen kompatibilis,
hogy custom repositoryként megosztható legyen.

Végül: futtass le mindent, és írd meg, mi az, amit hardver nélkül nem lehetett ellenőrizni.
```

---

## Amit érdemes közbeszúrni

**Az 1. prompt után, mielőtt továbbmész:** futtasd le a `tools/dump.py`-t a valós hardveren
(`192.168.1.37:502`, slave 3, a hőszivattyú bekapcsolva). Ez az egyetlen pont, ahol a
regisztertérkép igazolható. Ha eltérést látsz a JSON-hoz képest, azt előbb javítsd ki,
mint hogy bármi ráépüljön.

**A 3. prompt után:** először csak olvasd az entitásokat legalább egy napig, `write_level:
basic` mellett is kerüld az írást. Figyeld a BSB hibaszámlálókat (9916–9919) — ha nőnek,
a probléma a BSB kábelezésben van, nem a kódban. Az első írás a 42-es regiszter (HMV
csökkentett setpoint) ±1 °C legyen, ellenőrzéssel a hőszivattyú kijelzőjén.
