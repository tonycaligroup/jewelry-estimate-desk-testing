# Render archetypes

One JSON file per construction archetype: `construction` (how the piece
physically holds together), `photo` (camera and lighting), two `views`, and
the `checks` a vision model answers yes or no about each render. An optional
`<id>.exemplar.png` beside the file is passed to the image model as a
reference for construction. Adding a kind of piece is adding one file here;
no code changes.
