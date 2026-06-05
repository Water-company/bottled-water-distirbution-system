(function () {
    const DEFAULT_CENTER = [9.03, 38.74];

    function createIcon(kind, label) {
        return L.divIcon({
            className: "map-marker-wrapper",
            html: `<span class="map-marker-pin map-marker-pin--${kind}">${label}</span>`,
            iconSize: [36, 36],
            iconAnchor: [18, 36],
            popupAnchor: [0, -28],
        });
    }

    function createMap(elementId, options = {}) {
        const map = L.map(elementId, {
            zoomControl: options.zoomControl !== false,
        }).setView(options.center || DEFAULT_CENTER, options.zoom || 13);

        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            attribution: "&copy; OpenStreetMap contributors",
            maxZoom: 19,
        }).addTo(map);

        return map;
    }

    function fitMap(map, points, fallbackCenter = DEFAULT_CENTER, fallbackZoom = 13) {
        const validPoints = points.filter((point) => Array.isArray(point) && point.length === 2 && !Number.isNaN(point[0]) && !Number.isNaN(point[1]));
        if (!validPoints.length) {
            map.setView(fallbackCenter, fallbackZoom);
            return;
        }
        if (validPoints.length === 1) {
            map.setView(validPoints[0], Math.max(fallbackZoom, 15));
            return;
        }
        map.fitBounds(validPoints, { padding: [30, 30] });
    }

    function debounce(callback, wait = 300) {
        let timeoutId;
        return (...args) => {
            window.clearTimeout(timeoutId);
            timeoutId = window.setTimeout(() => callback(...args), wait);
        };
    }

    function escapeHtml(value) {
        return String(value)
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");
    }

    window.AquaFlowMaps = {
        createMap,
        fitMap,
        debounce,
        escapeHtml,
        icons: {
            customer: createIcon("customer", "C"),
            driver: createIcon("driver", "D"),
            agent: createIcon("agent", "A"),
            warehouse: createIcon("warehouse", "W"),
        },
        defaultCenter: DEFAULT_CENTER,
    };
})();
