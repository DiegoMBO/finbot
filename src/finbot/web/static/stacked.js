// Barras empilhadas por mês (faturas por cartão, entradas por tipo).
// Lê as tags script type="application/json" com data-stacked="ID_DO_DIV" e o JSON
// {months, series: [{name, slot, values}]}.
// A cor de cada série vem da variável --series-N (paleta validada), fixa por entidade.
(function () {
  "use strict";
  const brl = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const charts = [];

  function option(data) {
    const text = css("--text-2");
    const surface = css("--surface");
    return {
      animationDuration: 300,
      grid: { left: 8, right: 8, top: 34, bottom: 4, containLabel: true },
      legend: { top: 0, left: 0, itemWidth: 12, itemHeight: 12, textStyle: { color: text, fontSize: 12 } },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        confine: true,
        backgroundColor: surface,
        borderColor: css("--border"),
        textStyle: { color: css("--text"), fontSize: 12 },
        formatter: (ps) => {
          const total = ps.reduce((s, p) => s + p.value, 0);
          const rows = ps.filter((p) => p.value).map((p) => `${p.marker}${p.seriesName}: ${brl.format(p.value / 100)}`);
          return `<b>${ps[0].name}</b> · total ${brl.format(total / 100)}<br>${rows.join("<br>")}`;
        },
      },
      xAxis: {
        type: "category",
        data: data.months,
        axisLine: { lineStyle: { color: css("--grid") } },
        axisTick: { show: false },
        axisLabel: { color: text, fontSize: 11 },
      },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { color: css("--grid") } },
        axisLabel: { color: text, fontSize: 11, formatter: (v) => (v >= 100000 ? v / 100000 + " mil" : v / 100) },
      },
      series: data.series.map((s, i) => ({
        name: s.name,
        type: "bar",
        stack: "total",
        barMaxWidth: 36,
        color: css(`--series-${s.slot}`),
        // 2px da cor do fundo entre os pedaços empilhados; cantos só no topo
        itemStyle: { borderColor: surface, borderWidth: 1, borderRadius: i === data.series.length - 1 ? [4, 4, 0, 0] : 0 },
        data: s.values,
      })),
    };
  }

  function draw() {
    for (const tag of document.querySelectorAll("script[data-stacked]")) {
      const el = document.getElementById(tag.dataset.stacked);
      if (!el || !window.echarts) continue;
      const data = JSON.parse(tag.textContent);
      if (!data.months.length) continue;
      const chart = echarts.getInstanceByDom(el) || echarts.init(el, null, { renderer: "svg" });
      chart.setOption(option(data), true);
      if (!charts.includes(chart)) charts.push(chart);
    }
  }

  draw();
  window.addEventListener("resize", () => charts.forEach((c) => c.resize()));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
})();
