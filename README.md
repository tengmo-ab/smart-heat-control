# Smart Heat Control

**Pris- och väderstyrd optimering för värmepumpar, fjärrvärme och varmvattenberedare via Home Assistant.**
*(English summary below.)*

> ⚠️ **Status: pre-alpha (v0.1.0)** — Strukturen är på plats men logiken porteras fortfarande från en beprövad 2-årig YAML-automation. Använd inte i produktion än.

---

## Vad är detta?

Smart Heat Control är en HACS-integration som lägger ett intelligent styrlager *ovanpå* dina befintliga värme-entiteter i Home Assistant. Den läser ditt elpris (Nord Pool / Tibber / ENTSO-e), väderprognos, solel-överskott och kompressor-/tillsatseffekt, och skriver tillbaka till en `climate.*`-entitet, en `number.*`-entitet (värmekurva), och en `number.*`-entitet (varmvattenbörvärde) för att flytta el-konsumtionen till billiga timmar utan att förlora komfort.

Logiken kommer från en YAML-automation som körts dagligen i 2 år på en Comfortzone RX95 frånluftsvärmepump. v2 abstraherar bort märkesberoendet: integrationen jobbar mot *roller* (climate-entitet, värmekurv-number, varmvatten-number, kompressoreffekt-sensor osv.) som du pekar ut i ett config-flow vid installationen — den fungerar därmed mot Comfortzone, Nibe, IVT, Thermia, Mitsubishi, eller en kombination av fjärrvärme + separat varmvattenberedare.

## Designprinciper

1. **Allt är valfritt utom själva värmesystemet och utomhustemp.** Saknar du väderprognos? Logiken faller tillbaka på prisstyrning. Saknar du Nord Pool? Logiken kör väder-anteciperad reduktion. Saknar du både och? Integrationen sätter dina default-värden och avstår från optimering — komforten är säker, du förlorar bara besparingen.
2. **Tillfälligt trasiga sensorer ska inte krascha optimering.** Varje upstream-läsning som rapporterar `unavailable`/`unknown` eller är utanför rimligt intervall behandlas som `None`, och den specifika gren som behöver värdet inaktiveras tills nästa cykel. Andra grenar fortsätter köra.
3. **Den 2-åriga ordningsföljden är lag.** v2 är en 1:1-portering av kaskaden — inga "förbättringar" smygs in utan explicit godkännande. Buggar som hittas i v1 (t.ex. `hw_mode` sträng-vs-float-jämförelsen) fixas i v2 men dokumenteras separat.
4. **Stabila strängar för stats.** Modes (`Default`, `Cheap Price Intensify`, `Price Peak Reduction`, `Weather Anticipation Reduction`, `Mid-day Boost`, `Night Boost`, `Legionella Boost`, `Heating Priority`) byts inte utan migrationsplan eftersom de hamnar i HA:s state-historik.

## Arkitektur

```
custom_components/smart_heat_control/
├── manifest.json          ✅ HACS-metadata
├── const.py               ✅ DOMAIN, CONF_*, defaults, mode-strängar, tröskelvärden
├── config_flow.py         ✅ 6-stegs config-flow (heating → power → pricing → weather → solar → defaults)
├── models.py              ✅ Inputs / Computed / Decision / Health-dataclasser
├── coordinator.py         ⏳ DataUpdateCoordinator (skelett)
├── __init__.py            ✅ async_setup_entry / unload
├── computed.py            ⏳ Beräkningar porteras från v1 variables:-block
├── controller.py          ⏳ Beslutskaskaden porteras från v1 choose:-grenar
├── select.py              ⏳ current_optimization_mode, hw_mode (read-only)
├── switch.py              ⏳ master_enabled, *_enabled-flaggor
├── number.py              ⏳ default_*, threshold, legionella-tröskelvärden
├── datetime.py            ⏳ vacation_end, last_legionella_run
├── binary_sensor.py       ⏳ hw_reduction_active, current_hour_is_cheap m.fl.
├── sensor.py              ⏳ Ported template-sensorer (cheap_hours_list, AM/PM-snitt osv.)
├── button.py              ⏳ reset_to_defaults, trigger_legionella_now
├── strings.json           ⏳
└── translations/{en,sv}.json
```

## Roller (vad du pekar ut i config-flowet)

| Steg | Roll | Krav | Effekt om saknas |
| :-- | :-- | :-- | :-- |
| Heating system | `climate_entity` | **Krav** | — |
| Heating system | `outdoor_temp_sensor` | **Krav** | — |
| Heating system | `indoor_temp_sensor` | Valfri | Faller tillbaka på `climate.attributes.current_temperature` |
| Heating system | `heat_curve_number` | Valfri | Värmekurv-justering avstängd |
| Heating system | `hot_water_setpoint_number` | Valfri | All HW-styrning avstängd |
| Heating system | `hot_water_extra_switch` | Valfri | Extra-VV-detektering avstängd |
| Heating system | `hot_water_temp_sensor` | Valfri | Vissa HW-grenar förenklas |
| Heating system | `pump_activity_sensor` | Valfri | HW-reduktion + vissa boost-grenar avstängda |
| Power | `compressor_power_sensor` | Valfri | "Full kompressor"-detektering avstängd |
| Power | `aux_power_sensor` | Valfri | Tillsatsskydd avstängt — kan ge mer aux-drift |
| Pricing | `price_sensor` | **Krav för prisopt.** | Hela pris-grenen avstängd, väder-only |
| Pricing | `price_today_sensor` | Valfri | Dagsmedel härleds från `today`-attributet |
| Weather | `weather_forecast_entity` | Valfri | Månadsbaserad winter-fallback, väder-anticipation av |
| Solar/PV | `pv_excess_binary_sensor` | Valfri | Inga PV-överskotts-justeringar |
| Solar/PV | `solar_forecast_today_sensor` | Valfri | "Survive solar"-läget avstängt |
| Solar/PV | `battery_discharging_binary_sensor` | Valfri | Batteristatus inte med i beslut |
| Solar/PV | `bridge_to_solar_binary_sensor` | Valfri | Bridge-undantag i Price Peak avstängt |
| Solar/PV | `is_sunny_day_binary_sensor` | Valfri | Soldags-heuristik från `weather_forecast.condition` |

## Robusthetstrappa

```
allt fungerar               → full kaskad (Weather > Price Peak > Cheap > Default)
pris saknas tillfälligt     → väder-only (Weather Anticipation + Default)
väder saknas tillfälligt    → pris-only (Price Peak + Cheap + Default)
båda saknas                 → endast Default-grenen, dina default-värden, ingen optimering
indoor/outdoor temp saknas  → safe-mode: skriv inget, logga, vänta på återställning
```

Integrationen rapporterar nedgraderingen via `select.smart_heat_control_optimization_mode` så du ser vilken nivå den jobbar på.

## Roadmap

- [x] Skelett: manifest, const, config_flow, __init__, coordinator, models
- [ ] Computed-sensorer (porterar v1 `variables:`-block)
- [ ] Controller (porterar v1 kaskad)
- [ ] Entity-plattformar (sensor/switch/number/select/datetime/binary_sensor/button)
- [ ] Översättningar (en, sv)
- [ ] Testsvit (`tests/test_controller.py` med scenario per gren)
- [ ] Diagnostik (redacted dump)
- [ ] Stats-migrationsguide för existerande v1-användare

## Licens

[Apache License 2.0](LICENSE)

---

# 🇬🇧 English summary

Smart Heat Control is a Home Assistant integration that adds price- and weather-aware optimization on top of any heat pump, district heating system, or hot water boiler. It reads your spot price (Nord Pool / Tibber / ENTSO-e), weather forecast, PV surplus, and compressor/aux power, and writes back to a climate entity + heating-curve number + hot-water-setpoint number to shift consumption to cheap hours without losing comfort.

The logic is a 1:1 port of a 2-year-proven YAML automation originally written for a Comfortzone RX95 exhaust-air heat pump, abstracted in v2 to work with any vendor through entity-role bindings in the config flow.

**Core idea:** every upstream entity except the climate entity and outdoor temp is optional. Missing weather forecast? Price-only optimization. Missing price? Weather-only. Missing both? Safe defaults — comfort preserved, savings forfeited.

**Status: pre-alpha.** Don't run in production yet.
