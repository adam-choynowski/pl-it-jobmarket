# NOTES — rozpoznanie API justjoin.it

Dziennik pracy: co ustaliłem, czego dowiodłem, co okazało się fałszem i co nadal
jest zgadywaniem. Wszystkie liczby pochodzą z pomiarów z **2026-08-09**
(UTC, ~17:50–18:20).

---

## 1. Jak znalazłem API

1. `curl https://justjoin.it/job-offers/warszawa` → 200, ~2 MB HTML (Next.js /
   React Server Components). Danych ofert w HTML nie parsowałem — to ślepa
   uliczka pod kątem stabilności.
2. W HTML: `"baseApiUrl":"https://api.justjoin.it"` oraz `appName:"user-panel"`.
3. Pobrałem 46 chunków JS z `/_next/static/chunks/` i wygrepowałem ścieżki
   `"/vN/..."`. Znalezione istotne:
   - `/v2/user-panel/offers/by-cursor` — lista ofert (kursorowa),
   - `/v2/user-panel/offers/count` — licznik,
   - `/v2/user-panel/offers/all-boards/count` — licznik "wszystkie boardy",
   - `/v2/user-panel/offers/popular`, `/v1/offers/{slug}` — szczegóły oferty.
4. W kodzie klienta widać pełną listę parametrów `by-cursor`:
   `from, itemsCount, sortBy, orderBy, query, currency, categories, keywords,
   companyNames, skills, jobTitles, withSalary, remoteWorkOptions, remote, city,
   salaryMin, salaryMax, ukraineFriendly, experienceLevels, employmentTypes,
   workingTimes, cityRadiusKm, publishedSinceDays`
   (tablice serializowane jako `param[]=...`).
   Domyślne wartości UI: `{from: 0, itemsCount: 100, sortBy: "published", orderBy: "DESC"}`.

**Wybrany endpoint kolektora:**
```
GET https://api.justjoin.it/v2/user-panel/offers/by-cursor
    ?itemsCount=100&from=<N>&sortBy=newest&orderBy=ASC
```

Kształt odpowiedzi:
```json
{"data": [ {...oferta...} ],
 "meta": {"from":0,
          "prev":{"cursor":null,"itemsCount":100},
          "next":{"cursor":100,"itemsCount":100},
          "totalItems":10375}}
```

---

## 2. Nagłówki — minimalny zestaw i uzasadnienie

Wysyłam dokładnie trzy. Żadnych ciasteczek, tokenów, `Referer`, `Origin`.

| nagłówek | dlaczego zostaje |
|---|---|
| `User-Agent: jjit-collector/1.0 (daily job-market research; contact: <mail>)` | **konieczny**. Domyślny `Python-urllib/3.9` dostaje **HTTP 403** od Cloudflare (sprawdzone). Dowolny „normalny" UA przechodzi (nawet pusty i domyślny curlowy dają 200) — więc skoro i tak muszę coś wysłać, wysyłam string identyfikujący projekt i kontakt. |
| `Accept: application/json` | deklaracja oczekiwanej reprezentacji; nic nie kosztuje, chroni przed ewentualną negocjacją treści w przyszłości. |
| `Accept-Encoding: gzip` | **grzeczność wobec serwisu**: strona to 26 kB zamiast 166 kB (6×). Cały przebieg: ~2,7 MB zamiast ~18 MB transferu. `urllib` nie rozpakowuje sam — robię to jawnie. |

Sprawdzone empirycznie:
- brak `User-Agent` w ogóle (curl `-H "User-Agent:"`) → 200,
- domyślny UA curla → 200,
- **`Python-urllib/3.9` → 403** ← to jest powód, dla którego UA jest obowiązkowy.

Endpoint działa w pełni anonimowo — żadne ciasteczko nie jest potrzebne.

---

## 3. Hipotezy, które okazały się BŁĘDNE

### 3.1 „Domyślne sortowanie jest w porządku, to tylko kolejność" — FAŁSZ

Pierwsze okno z domyślnym sortowaniem UI (`sortBy=published`) zwraca **83, nie
100 rekordów**, mimo że w bazie jest 10 375:

```
from=0&itemsCount=100&sortBy=published  -> 83 rekordy
from=0&itemsCount=20 &sortBy=published  ->  3 rekordy   (!)
from=100&itemsCount=100&sortBy=published -> 100 rekordów
```

Kursor przesuwa się o `itemsCount` **niezależnie** od tego, ile rekordów wróciło.
Czyli: przy domyślnym sortowaniu **cicho gubię ~17 ofert** i nic mi tego nie
zgłasza. Najprawdopodobniej to sloty promowane (`isSuperOffer` / `promotedPosition`)
wstrzykiwane na górę listy i deduplikowane po wygenerowaniu okna.

**Jak wykryłem:** porównałem `len(data)` z żądanym `itemsCount` na pierwszej
stronie. Gdyby nie ten test, kolektor codziennie gubiłby kilkanaście ofert
i raportował sukces.

**Konsekwencja:** `sortBy=newest` — tam okno zawsze zwraca pełne 100.

### 3.2 „Kolejność domyślna jest stabilna, więc mogę stronicować" — FAŁSZ

```
okno [0,100)                      = 83 rekordy
okno [0,50) + [50,100)            = 33 + 50 = 83 rekordy
```
Liczby się zgadzają, ale **zbiory nie**: 18 rekordów różnicy w każdą stronę.
Ta sama pozycja listy zwraca różne oferty przy różnym podziale na okna →
kolejność `published` nie jest totalnym porządkiem. Stronicowanie offsetowe po
niestabilnej kolejności **gubi i duplikuje** rekordy.

`sortBy=newest` jest stabilne: dwa identyczne żądania dają identyczną listę,
a `lastPublishedAt` jest monotoniczne na **całych** 10 375 rekordach
(sprawdzam to jako check w skrypcie).

### 3.3 „`/offers/count` zwraca liczbę ofert" — FAŁSZ

```
/v2/user-panel/offers/count            -> 19713
by-cursor meta.totalItems              -> 10375
/v2/user-panel/offers/count?city=warszawa -> 13509   (więcej niż cała Polska!)
```
Trzecia liczba to był sygnał ostrzegawczy — Warszawa nie może mieć więcej ofert
niż całość. Sprawdziłem, czym jest 19 713:

```python
sum(len(o["multilocation"]) for o in wszystkie_10375_ofert) == 19713   # dokładnie
```

**`/offers/count` liczy wiersze oferta×lokalizacja**, nie oferty. Oferta
wystawiona w 5 miastach liczy się 5×. Rozkład: 7 746 ofert w 1 mieście,
581 w 2, 410 w 3, 298 w 4, 1 003 w 5, 179 w 10.

To nie jest wada — to **niezależny świadek kompletności** (inny endpoint, inna
agregacja) i używam go jako liczby kontrolnej.

(13 509 dla Warszawy nadal nie ma dla mnie pełnego wyjaśnienia — patrz §7.)

### 3.4 „`itemsCount` można podkręcić" — FAŁSZ, ale głośno

`itemsCount=200` → `HTTP 400 {"message":["itemsCount must not be greater than 100"]}`.
Limit twardy, walidowany po stronie serwera. 100 to maksimum.

### 3.5 „Paginacja offsetowa urwie się na jakiejś głębokości" — na szczęście NIE

Typowa pułapka (Elasticsearch `max_result_window` ≈ 10 000). Sprawdziłem
punktowo, zanim napisałem crawler:

```
from=5000  -> 100 rekordów
from=10000 -> 100 rekordów      (przekracza 10k — brak limitu)
from=10300 ->  75 rekordów, next.cursor=null
from=10375 ->   0 rekordów
from=20000 ->   0 rekordów
```
Paginacja sięga dokładnie do `totalItems`. Żadnego ukrytego sufitu.

### 3.6 „Literówka w nazwie parametru wyjdzie jako błąd" — FAŁSZ

```
?sortBy=bogus  -> HTTP 400 "sortBy must be a valid enum value"    (głośno)
?orderBy=asc   -> HTTP 400 "orderBy must be a valid enum value"   (głośno, enum jest case-sensitive: ASC/DESC)
?bogusParam=xyz-> 200, ignorowane                                 (cicho)
?sortby=newest -> 200, 83 rekordy zamiast 100                     (CICHO I GROŹNIE)
```

Zła **wartość** wywala się od razu. Zła **nazwa** parametru jest po cichu
ignorowana i cofa mnie do wadliwego sortowania domyślnego z §3.1. Dokładnie ten
przypadek jest testem walidacji (kryterium odbioru nr 4) — patrz §6.

### 3.7 „Jest jedno API ofert" — FAŁSZ (znalezione po fakcie, 2026-08-09 18:35)

Podczas grepowania chunków widziałem **drugiego** klienta API (`3mmdo8alkrbyc.js`)
ze ścieżkami `/offers`, `/offers/count`, `/offers/all-boards/count`,
`/company-profiles/...`. **Nie rozwiązałem jego base URL i nie sprawdziłem go** —
uznałem, że skoro `/v2/user-panel/*` działa, temat jest zamknięty. To był błąd
metodyczny: porzuciłem wątek, zamiast go domknąć.

To jest nowszy gateway: **`https://justjoin.it/api/candidate-api/offers`**.
Sprawdzony teraz, i ma pułapkę, której mój endpoint nie ma:

```
GET /api/candidate-api/offers?itemsCount=1&from=0
    -> meta.totalItems = 10000        <-- OKRĄGŁA LICZBA, nie fakt
GET /api/candidate-api/offers/count
    -> 19335
from=9990  -> 200, n=10, next.cursor=10000
from=10000 -> HTTP 500 Internal Server Error
from=10001 -> HTTP 500
from=12000 -> HTTP 500
```

`totalItems: 10000` to **twardy sufit podany jako fakt**, a nie liczba rekordów.
Za nim API nie zwraca pustej strony — wywala się 500. Kolektor oparty na tym
endpoincie i ufający `totalItems` zebrałby 10 000 rekordów, uznał, że ma komplet,
i zakończył się kodem 0.

**Dlaczego mnie to nie dotyczy:** stary `by-cursor` deklaruje `totalItems=10375`
(liczba nieokrągła) i faktycznie do niej dochodzi — ostatnia strona ma 75
rekordów i `next.cursor=null`, a `from=20000` zwraca pustą listę zamiast 500.
Sufit produkuje okrągłą liczbę i pełną ostatnią stronę; ja mam odwrotnie.
Test z §3.5 był właściwym testem — po prostu wykonałem go na endpoincie,
który akurat sufitu nie ma.

### 3.8 Fasety i test rozłączności — narzędzie, którego nie znalazłem

Nowy gateway ma **`/api/candidate-api/offers/facets/count`**. Zwraca liczniki
w rozbiciu na wymiary. Sumy są rozstrzygające:

| faseta | suma liczników | interpretacja |
|---|---|---|
| `workplaceTypes` (office 804, hybrid 7474, remote 11057, mobile 0) | **19 335** | rozłączny podział — pokrywa całą populację |
| `workingTimes` | **19 335** | rozłączny podział |
| `experienceLevels` | **19 335** | rozłączny podział |
| `employmentTypes` (b2b 15660, permanent 8295, …) | **31 127** | **wielowartościowa** — suma > populacji |
| `languages` | 16 370 | < populacji (oferty bez języka) |
| `publishedSinceDays` (1/7/14/30) | 18 923 | kubełki **kumulatywne**, nie podział |

To jest technika, której u siebie nie zastosowałem: **suma rozłącznej fasety musi
równać się populacji**. Trzy niezależne wymiary dają dokładnie 19 335, co dowodzi
prawdziwego rozmiaru populacji niezależnie od skłamanego `totalItems: 10000`.
Równocześnie `employmentTypes` pokazuje, dlaczego nie wolno tego robić na
oślep — na wielowartościowym wymiarze test daje 31 127 i wygląda jak „awaria",
choć wszystko jest w porządku.

### 3.9 Czym naprawdę jest 19 335 (i dlaczego to nie 19 713)

Zrekoncyliowałem fasety z moim archiwum. Rozkład `workplaceType` liczony
**per oferta** vs **per wiersz oferta×lokalizacja**:

| typ | per oferta (moje) | per lokalizacja (moje) | faceta candidate-api |
|---|---|---|---|
| office | 704 | 815 | 804 |
| hybrid | 5 373 | 7 645 | 7 474 |
| remote | 4 298 | 11 253 | 11 057 |
| **suma** | **10 375** | **19 713** | **19 335** |

Kolumna per-lokalizacja zgadza się z fasetami w granicach ~2 %; kolumna per-oferta
mija się o 2,5×. **Wniosek: fasety liczą wiersze oferta×lokalizacja, tak samo jak
stary `/offers/count`.** Czyli 19 335 to nie „prawdziwa liczba ofert" — to liczba
wierszy lokalizacyjnych na nowym gatewayu, odpowiednik moich 19 713.
To samo potwierdza `experienceLevels` (moje per-lokalizacja: senior 10 945,
mid 6 918, junior 614 vs fasety 10 745 / 6 787 / 582).

Różnica **19 713 vs 19 335 (1,9 %)** jest systematyczna — moje liczby są wyższe
w *każdym* wymiarze. Nowy gateway indeksuje nieco mniejszą (lub opóźnioną)
populację. Nie ustaliłem, którą dokładnie; **mój zbiór jest nadzbiorem**, więc
nie gubię przez to danych, ale to jest otwarte pytanie (§7).

### 3.10 `employmentTypes` jest wielowartościowe

Nie odnotowałem tego wcześniej. W moich danych:

```
liczba wpisów employmentTypes na ofertę:  1 -> 9405 ofert,  2 -> 970 ofert
suma wpisów: 11 345 przy 10 375 ofertach
najczęstsza kombinacja: (b2b, permanent) — 889 ofert
                        (b2b, mandate_contract) — 63, (mandate_contract, permanent) — 13
```

Jedna oferta może być jednocześnie B2B i UoP, z osobnymi widełkami dla każdej
formy. **Konsekwencja dla warstwy analitycznej:** `employmentTypes` nie może być
kolumną w tabeli ofert — to relacja 1:N, tak samo jak `multilocation`.
Naiwne `offer.employmentTypes[0].from` jako „wynagrodzenie" zgubi 970 ofert
i systematycznie przekłamie statystyki B2B vs UoP.

Uwaga o kształcie: w `candidate-api` ta sama tablica zawiera dodatkowo warianty
walutowe tego samego `type` (CHF/USD/EUR z `currencySource: "conversion"`),
więc jej długość ≠ liczba form zatrudnienia. W starym `by-cursor` przeliczniki
są polami w jednym wpisie (`fromEur`, `fromUsd`, …). Dwa różne kształty tego
samego pojęcia — kolejny powód, żeby nie mieszać źródeł w jednym archiwum.

---

## 4. Dlaczego `orderBy=ASC`, a nie `DESC`

Oba działają i oba są stabilne. Wybrałem **ASC** (od najstarszych), bo przebieg
trwa ~80 s i lista może się w tym czasie zmienić:

- przy `DESC` nowa oferta wchodzi **na początek** listy → wszystko przesuwa się
  o jeden w dół → okno, które już pobrałem, przykrywa rekord, którego nigdy nie
  zobaczę (cicha strata),
- przy `ASC` nowa oferta ląduje **na końcu**, za kursorem → w najgorszym razie
  złapię ją lub nie w tym przebiegu, ale nic nie ginie z już przetworzonej części.

Usunięcie oferty w trakcie przebiegu przesuwa listę w obie stronę tak samo —
tego nie da się uniknąć offsetem, dlatego jest na to check (§5, C2/C6) i ponowny
pełny przebieg.

---

## 5. Liczby kontrolne i checki

Skrypt zapisuje plik **tylko wtedy**, gdy wszystkie poniższe przejdą. Każdy
nieudany check → `exit 1`, komunikat na stderr, brak pliku.

| check | co dowodzi | skąd liczba |
|---|---|---|
| `totalItems_stable_across_pages` | API przez cały przebieg deklarowało jedną liczbę | `meta.totalItems` z każdej ze 104 stron |
| `collected_equals_totalItems` | zebrałem tyle, ile API twierdzi, że istnieje | 10 375 == 10 375 |
| `no_duplicate_guids` | okna nie nachodzą na siebie (brak przesunięcia listy) | 10 375 unikatowych `guid` |
| `no_missing_guids` | każdy rekord ma identyfikator | — |
| `page_count_matches_total` | żadna strona nie została pominięta | `ceil(10375/100) == 104` |
| `last_page_terminates` | doszedłem do końca, a nie do limitu | ostatnie `next.cursor == null` |
| `sort_key_monotonic_asc` | kolejność jest **totalna** → okna kafelkują listę bez dziur | `lastPublishedAt` niemalejące na 10 375 rekordach |
| `location_rows_match_count_endpoint` | **niezależne potwierdzenie z innego endpointu** | `sum(len(multilocation)) == /offers/count`, zmierzone przed i po przebiegu: 19 713 == 19 713 == 19 713 |

Ostatni check jest najmocniejszy: `/offers/count` liczy inaczej i osobno, więc
zgodność co do jednego wiersza oznacza, że nie zgubiłem ani jednej oferty
wielolokalizacyjnej ani jednolokalizacyjnej. Jest też czuły na zmianę tablicy
ogłoszeń w trakcie przebiegu (`before != after` → retry).

**Odporność na churn:** niezgodność (`exit 1`) jest najpierw traktowana jako
możliwa zmiana tablicy w trakcie przebiegu — skrypt powtarza **cały** przebieg
do 3 razy (30 s przerwy). Prawdziwa awaria zawala się tak samo za każdym razem.
Nigdy nie zapisuje „częściowego" dnia.

---

## 6. Test walidacji (kryterium odbioru nr 4)

```
python3 collect.py --sort-param-name sortby --out-dir recon/sabotage
```
Podmienia nazwę parametru `sortBy` → `sortby` (API ignoruje, wraca do sortowania
domyślnego). Wynik: patrz §6.1 — walidacja wywala się, kod wyjścia ≠ 0,
katalog wyjściowy pozostaje pusty.

### 6.1 Wynik faktycznego uruchomienia

```
$ python3 collect.py --sort-param-name sortby --out-dir recon/sabotage
[18:07:13] completeness validation FAILED:
  - collected_equals_totalItems — collected=10358 expected=10375
  - no_duplicate_guids — unique=10357 collected=10358
  - sort_key_monotonic_asc — order breaks at index 0 (2026-08-04T22:10:25.925Z > 2026-05-11T14:07:35.826Z)
  - location_rows_match_count_endpoint — sum(multilocation)=19689 count_before=19713 count_after=19713
[18:07:13] retrying with a fresh snapshot in 30s
...
FATAL: completeness validation FAILED:
  (identycznie, we wszystkich 3 próbach)

$ echo $?
1
$ ls recon/sabotage
ls: recon/sabotage: No such file or directory
```

Cztery z ośmiu checków zapaliły się niezależnie od siebie. **Nie powstał żaden
plik** — nawet katalog wyjściowy nie został utworzony. Trzy próby zawiodły
identycznie, co odróżnia prawdziwą awarię od chwilowej zmiany tablicy ogłoszeń.

Zwróć uwagę na skalę cichej straty, gdyby walidacji nie było: **10 358 zamiast
10 375** — 17 ofek mniej i jeden duplikat, przy `exit 0` i pozornie normalnym
logu (104 strony, wszystkie HTTP 200). Dokładnie taki błąd zjadłby dane
z sierpnia bez śladu.

---

## 7. Czego NIE ustaliłem / co jest zgadywaniem

- **Dlaczego `/offers/count?city=warszawa` = 13 509**, skoro `by-cursor` z tym
  samym filtrem deklaruje `totalItems=5442`, a wierszy oferta×lokalizacja jest
  w całej bazie 19 713. Hipoteza (niesprawdzona): licznik z filtrem miasta
  domyślnie dokłada promień wyszukiwania (`cityRadiusKm`, UI ma domyślne 0/30 km)
  albo liczy po wszystkich boardach. **Nie ma to wpływu na kolektor** — nie
  filtruję po mieście i nie używam tej liczby.
- **`/offers/all-boards/count` = 46 613** — to najpewniej justjoin.it +
  rocketjobs.pl + inne tablice tej samej grupy. Poza zakresem.
- **„Cała Polska":** kolektor **nie filtruje po kraju ani mieście** — bierze
  całą listę, która zawiera też oferty zagraniczne (pierwszy rekord w próbce:
  Mafra, Portugalia). To celowe: warstwa surowa ma być nadzbiorem, filtrowanie
  należy do warstwy analitycznej. Miasta są w danych niespójne (`Warszawa` 4577 vs
  `Warsaw` 350, `Kraków` 1205 vs `Krakow` 226) — to problem normalizacji, nie zbierania.
- **Czy `totalItems` bywa kłamliwe** — TAK, ale na drugim gatewayu:
  `candidate-api` podaje `10000` i to jest sufit, nie liczba (§3.7). Na moim
  endpoincie `totalItems=10375` zgadzało się co do jednego z faktycznie pobraną
  liczbą i z niezależnym licznikiem. Nie ufam mu samemu, dlatego stoi obok
  siedmiu innych checków.
- **Skąd różnica 19 713 (stary `/count`) vs 19 335 (`candidate-api`)** — 1,9 %,
  systematycznie na moją korzyść we wszystkich wymiarach (§3.9). Hipotezy
  niesprawdzone: opóźnienie indeksu nowego gatewaya, inny zestaw boardów, albo
  inna definicja „aktywnej" oferty. Do rozstrzygnięcia: pobrać obie listy w tej
  samej minucie i porównać zbiory `guid`. **Nie zrobiłem tego.**
- **Czy warto dołożyć fasety jako dziewiąty check** — prawdopodobnie tak, ale
  **nie jako równość**: przy 1,9 % rozjazdu między gatewayami sztywne `==`
  wywalałoby kolektor codziennie. Sensowniejszy byłby przedział tolerancji
  (np. ±5 %) plus twarde `==` na sumach faset *między sobą*
  (`workplaceTypes == workingTimes == experienceLevels`), bo to jest niezmiennik
  wewnętrzny jednego źródła. Świadomie tego jeszcze nie wdrożyłem.
- **Rate limit** — nie natrafiłem na 429 przy 0,5 s odstępu i ~104 żądaniach.
  Nie testowałem, gdzie jest granica; celowo. Kod obsługuje 429 z backoffem.
- **Trwałość API** — `/v2/user-panel/*` to prywatne API frontendu, bez
  gwarancji. Może zniknąć bez zapowiedzi. Checki wychwycą zmianę kształtu
  odpowiedzi (brak `data`/`meta` → `exit 1`), ale nie naprawią jej same.
- **Pełna treść oferty** (opis, wymagania) jest dopiero pod `/v1/offers/{slug}` —
  to 10 375 dodatkowych żądań dziennie. Świadomie poza zakresem tego kolektora.

---

## 8. Format archiwum

`data/raw/justjoin/YYYY-MM-DD.json.gz` — gzip nad jednym dokumentem JSON:

```jsonc
{
  "schema": "justjoin-raw/1",
  "collected_at_utc": "...", "finished_at_utc": "...", "duration_s": 76.6,
  "collector": {"script","user_agent","python","request_delay_s"},
  "request":   {"endpoint","count_endpoint","sort_by","order_by","page_size","urls":[...104]},
  "control_numbers": {
     "offers_collected": 10375, "offers_expected_totalItems": 10375,
     "unique_guids": 10375, "pages": 104,
     "location_rows_from_offers": 19713,
     "location_rows_count_endpoint_before": 19713,
     "location_rows_count_endpoint_after": 19713,
     "payload_sha256": "..."
  },
  "checks": [ {"name": ..., "ok": true, "detail": ...}, ... ],
  "pages": [ {"url","http_status","fetched_at_utc","body": "<DOKŁADNY tekst odpowiedzi>"} ]
}
```

`body` to **string** z surową odpowiedzią, nie sparsowany obiekt — nic nie jest
przepisywane, przenumerowane ani porzucone (JSON re-serializowany zmieniłby
kolejność kluczy i formatowanie liczb). Parsowanie służy wyłącznie walidacji.
Zapis jest atomowy (`tmp` + `os.replace`) i następuje **po** walidacji, więc
uszkodzony plik nie może udawać dobrego dnia. Gzip z `mtime=0` — identyczne
wejście daje identyczny bajtowo plik.

Rozmiar: ~18 MB surowo → **~2,3 MB spakowane** na dzień (~840 MB/rok).

---

## 9. Co zostało faktycznie uruchomione

| przebieg | interpreter | wynik | log |
|---|---|---|---|
| 1 | 3.9.6 | exit 0, 10 375 ofert | `logs-recon/run1.log` |
| 2 (od razu po 1) | 3.9.6 | exit 0, 10 375 ofert, **zbiór `guid` identyczny co do rekordu** (0 różnic w obie strony) | `logs-recon/run2.log` |
| 3 (czysty katalog, `rm -rf data`) | 3.9.6 | exit 0, 10 375 ofert | `logs-recon/run3.log` |
| 4 | **3.12.13** | exit 0, 10 375 ofert | `logs-recon/run312.log` |
| sabotaż (`--sort-param-name sortby`) | 3.9.6 | **exit 1, brak pliku**, 3 próby zawiodły identycznie | `logs-recon/sab.log` |

Skrypt korzysta wyłącznie z biblioteki standardowej — brak zależności do
instalowania w CI. Czas przebiegu ~75 s (104 żądania × 0,5 s odstępu).

Uruchomienie: `python3 collect.py` (opcjonalnie `--date YYYY-MM-DD`,
`--out-dir …`, `JJIT_CONTACT=…` w środowisku).
