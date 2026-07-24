# Figure: documents per year, Commission vs other EU institutions. Run from repo root; writes figure-50-years.png
library(tidyverse)
library(arrow)

surface <- "#fcfcfb"
ink <- "#0b0b0b"
ink2 <- "#52514e"
muted <- "#898781"
grid <- "#e1e0d9"
blue <- "#56B4E9"
orange <- "#E69F00"

other_inst <- c(
  "PRES", "CJE", "STAT", "BEI", "CES", "COR", "ECA",
  "EO", "OLAF", "EDPS", "EPSO", "PESC", "DOC"
)

docs <- read_parquet("data/press-corner.parquet") |>
  mutate(
    year = year(as.Date(date)),
    source = if_else(doc_type %in% other_inst,
      "Other EU institutions (RAPID legacy)", "European Commission"
    )
  ) |>
  count(year, source) |>
  mutate(source = factor(source, levels = c(
    "Other EU institutions (RAPID legacy)", "European Commission"
  )))

p <- ggplot(docs, aes(year, n, fill = source)) +
  geom_col(width = 0.78, colour = surface, linewidth = 0.25) +
  annotate("text",
    x = 1991, y = 4870, hjust = 0, vjust = 1, lineheight = 1.15,
    label = "RAPID legacy series:\nCouncil, Court of Justice, Eurostat,\nEIB & more — until ~2014",
    colour = ink2, size = 3.4
  ) +
  annotate("segment",
    x = 1995.5, xend = 2001.4, y = 3960, yend = 3390,
    colour = muted, linewidth = 0.4
  ) +
  annotate("text",
    x = 1975, y = 330, hjust = 0, vjust = 0, lineheight = 1.15,
    label = "1975–1984:\nonly European Council\ndigests, a few per year",
    colour = ink2, size = 3.4
  ) +
  scale_fill_manual(values = c(
    "European Commission" = blue,
    "Other EU institutions (RAPID legacy)" = orange
  ), breaks = c("European Commission", "Other EU institutions (RAPID legacy)")) +
  scale_x_continuous(breaks = seq(1975, 2025, 10), expand = expansion(add = c(1, 1))) +
  scale_y_continuous(
    breaks = seq(0, 5000, 1000),
    labels = scales::label_comma(),
    expand = expansion(mult = c(0, 0.04))
  ) +
  labs(
    title = "Fifty years of EU press communication in one dataset",
    subtitle = "Documents per year in the European Commission Press Corner archive — 130,544 documents, March 1975 to July 2026",
    caption = "Data: EC Press Corner · built with presscorner-builder (github.com/tseidl/presscorner-builder) · dataset DOI: 10.5281/zenodo.21536427",
    x = NULL, y = NULL, fill = NULL
  ) +
  theme_minimal(base_size = 12, base_family = "Crimson Text") +
  theme(
    plot.background = element_rect(fill = surface, colour = NA),
    panel.grid.major.x = element_blank(),
    panel.grid.minor = element_blank(),
    panel.grid.major.y = element_line(colour = grid, linewidth = 0.35),
    axis.text = element_text(colour = muted, size = 9.5),
    legend.position = "top",
    legend.justification = "left",
    legend.margin = margin(t = 2, b = 2, l = -8),
    legend.key.size = unit(11, "pt"),
    legend.text = element_text(colour = ink2, size = 10),
    plot.title = element_text(colour = ink, face = "bold", size = 16),
    plot.subtitle = element_text(colour = ink2, size = 10.5, margin = margin(t = 3, b = 2)),
    plot.caption = element_text(colour = muted, size = 8, hjust = 0, margin = margin(t = 10)),
    plot.margin = margin(16, 20, 12, 20)
  )

ggsave(
  "figure-50-years.png",
  p,
  width = 9.5, height = 5.6, dpi = 300, device = ragg::agg_png, bg = surface
)
