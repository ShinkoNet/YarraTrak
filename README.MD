# YarraTrak

<p align="center">
  <img src="pebble/resources/images/yarratrak-transparent-full.png" alt="YarraTrak" width="240">
</p>

**Live Melbourne public transport departures on your Pebble smartwatch.**

YarraTrak shows live train, tram, and V/Line departure times directly on your wrist. Configure your favourite stations and get instant, glanceable info. To find out how long you've got before a service leaves, the app sends vibration patterns that encode the wait time, so you are aware of the time even when you're not looking at the watch.

## Features

- Live PTV departures with a second-by-second countdown
- Favourite stations for daily trips
- Platform info where PTV provides it
- Train distance info for metro services
- Vibration patterns for hours, tens of minutes, and minutes
- Support for trains, trams, and V/Line
- Up to 10 configured favourites

## Getting Started

Install YarraTrak from the Rebble Appstore, open it once, then use Settings on your phone to choose your favourite routes.

The app includes a few demo entries so you can see live departures straight away.

## Self Hosting

YarraTrak uses a small FastAPI server for PTV data and the Pebble settings page. If you run your own server, set your PTV developer ID and API key, install the Python requirements, and run `uvicorn server.api:app`.

Build the Pebble app from the `pebble` directory with the Pebble SDK:

```bash
pebble build
pebble install --phone YOUR_PHONE_IP
```

## Dashboard

The server includes a small internal dashboard for checking request volume, query volume, cache state, and recent service health. It is meant for the person running the server, not for public app users.

## API

The watch talks to the server over WebSocket for live favourite updates. The server also exposes simple HTTP endpoints for station lookup, favourite departure checks, and text queries.

## Bring Your Own Key

The optional Ask feature uses a key supplied by the user. Add an OpenRouter key in app settings if you want natural-language transport questions. If no key is set, the normal departure and vibration features still work.

## Vibrations

The vibration pattern encodes the wait time:

- hours are long buzzes
- tens of minutes are medium buzzes
- minutes are short buzzes

For example, 1 hour and 23 minutes feels like one long buzz, two medium buzzes, then three short buzzes.

When the service is about to leave, the app switches to a short "now" pattern. If the ETA slips backwards, the watch gives a long pulse so you can feel the delay without looking.

## Demo Entries

1. Flinders St to Belgrave
2. Caulfield to Town Hall
3. Barkly Sq/Sydney Rd to QVM/Elizabeth St
4. Southern Cross to Bendigo

## License

MIT
