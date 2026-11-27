// Detalhe da dívida: gráfico (histórico + projeção) e simulador desta dívida
// (/api/dividas/{id}/simular, as contas do /simular).
(function () {
  "use strict";

  const brl = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
  const fmt = (c) => brl.format(c / 100);
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const data = JSON.parse(document.getElementById("debt-data").textContent);
  const MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];
  let chart = null;

  function draw() {
    const el = document.getElementById("debt-chart");
    if (!el || !window.echarts) return;
    chart = chart || echarts.init(el, null, { renderer: "svg" });
    const hist = data.history.map((h) => [h.date, h.cents / 100]);
    // A projeção começa no último ponto do histórico, para as linhas se encontrarem.
    const proj = data.projection.length && hist.length
      ? [hist[hist.length - 1], ...data.projection.map((p) => [p.date, p.cents / 100])]
      : [];
    const text = css("--text-2");
    chart.setOption(
      {
        animationDuration: 300,
        grid: { left: 8, right: 12, top: 30, bottom: 4, containLabel: true },
        legend: { top: 0, left: 0, textStyle: { color: text, fontSize: 12 } },
        tooltip: {
          trigger: "axis",
          confine: true,
          backgroundColor: css("--surface"),
          borderColor: css("--border"),
          textStyle: { color: css("--text"), fontSize: 12 },
          valueFormatter: (v) => brl.format(v),
        },
        xAxis: {
          type: "time",
          axisLine: { lineStyle: { color: css("--grid") } },
          // Meses em português (o ECharts usaria inglês: "Dec/26")
          axisLabel: {
            color: text,
            fontSize: 11,
            hideOverlap: true,
            formatter: (v) => {
              const d = new Date(v);
              return `${MONTHS[d.getMonth()]}/${String(d.getFullYear()).slice(2)}`;
            },
          },
          splitLine: { show: false },
        },
        yAxis: {
          type: "value",
          min: 0,
          splitLine: { lineStyle: { color: css("--grid") } },
          axisLabel: { color: text, fontSize: 11, formatter: (v) => (v >= 1000 ? v / 1000 + " mil" : v) },
        },
        series: [
          { name: "Histórico", type: "line", step: "end", symbolSize: 8, color: css("--accent"), lineStyle: { width: 2.5 }, data: hist },
          { name: "Projeção", type: "line", symbol: "none", color: text, lineStyle: { width: 2, type: "dashed" }, data: proj },
        ],
      },
      true
    );
  }

  const slider = document.getElementById("extra");
  const result = document.getElementById("sim-result");
  let timer = null;

  function describe(r) {
    const side = (p) => {
      if (!p.end) return p.status === "crescendo" ? "não acaba: a dívida cresce" : "não acaba";
      return `acaba em ${p.end} (${p.months}x)` + (p.interest ? `, juros ${fmt(p.interest)}` : "");
    };
    if (!r.extra) return `Hoje: ${side(r.base)}.`;
    let text = `Com +${fmt(r.extra)}/mês: ${side(r.with_extra)}.`;
    if (r.months_saved !== null && r.months_saved !== undefined) {
      text += ` Termina ${r.months_saved} mês(es) antes`;
      text += r.interest_saved ? ` e economiza ${fmt(r.interest_saved)} de juros.` : ".";
    } else if (!r.base.end && r.with_extra.end) {
      text += " Com esse extra ela passa a ter data para acabar.";
    }
    return text;
  }

  async function simulate() {
    const extra = Number(slider.value);
    document.getElementById("extra-label").textContent = fmt(extra);
    const response = await fetch(`/api/dividas/${data.id}/simular?extra=${extra}`, { credentials: "same-origin" });
    if (response.status === 401) { window.location = "/login"; return; }
    result.textContent = describe(await response.json());
  }

  if (slider) {
    slider.addEventListener("input", () => {
      document.getElementById("extra-label").textContent = fmt(Number(slider.value));
      clearTimeout(timer);
      timer = setTimeout(simulate, 150);
    });
    simulate();
  }
  draw();
  window.addEventListener("resize", () => chart && chart.resize());
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
})();
