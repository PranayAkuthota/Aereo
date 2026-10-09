# Sample files

Both files are synthetic, contain no personal/survey data, and are safe to share.

- `survey.kml`: a small polygon near 78°E, 17°N, a short east–west line, and a point.
  Uses WGS84, has Placemark IDs/names, and an ExtendedData attribute. Expect three
  results: two measured and one not applicable. The polygon's area is roughly
  11,800m² and the line's length roughly 107m (projection scale affects both).
- `projected-square.zip`: one clockwise 100m × 100m square in EPSG:32643, with
  matching SHP/SHX/DBF/PRJ and attribute `label=synthetic`. Expect area 10,000m².

These are fixture coordinates, not evidence of a real property boundary.
