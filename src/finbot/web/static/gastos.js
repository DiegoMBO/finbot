// Gráficos da tela de Gastos (ECharts): por categoria (barras horizontais) e
// gastos dos últimos 6 meses (colunas). Uma série cada: sem legenda; o título diz.
(function () {
  "use strict";

  const brl = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
  const fmt = (cents) => brl.format(cents / 100);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const data = JSON.parse(document.getElementById("gastos-data").textContent);
  const charts = [];

  function base() {
    return {
      animationDuration: 400,
      textStyle: { fontFamily: "inherit" },
      tooltip: {
        confine: true,
        backgroundColor: css("--surface"),
        borderColor: css("--border"),
        textStyle: { color: css("--text"), fontSize: 12 },
      },
    };
  }

  function categories(el) {
    const items = [...data.categories].reverse(); // maior no topo
    return {
      ...base(),
      grid: { left: 8, right: 64, top: 4, bottom: 4, containLabel: true },
      tooltip: { ...base().tooltip, trigger: "item", formatter: (p) => `${p.name}: <b>${fmt(p.value)}</b> (${items[p.dataIndex].pct}%)` },
      xAxis: { type: "value", show: false },
      yAxis: {
        type: "category",
        data: items.map((c) => c.name),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: css("--text"), fontSize: 12 },
      },
      series: [
        {
          type: "bar",
          data: items.map((c) => c.cents),
          barMaxWidth: 18,
          itemStyle: { color: css("--accent"), borderRadius: [0, 4, 4, 0] },
          label: {
            show: true,
            position: "right",
            color: css("--text-2"),
            fontSize: 11,
            formatter: (p) => fmt(p.value).replace("R$", "").trim(),
          },
        },
      ],
    };
  }

  function history() {
    return {
      ...base(),
      grid: { left: 8, right: 8, top: 16, bottom: 4, containLabel: true },
      tooltip: { ...base().tooltip, trigger: "axis", axisPointer: { type: "shadow" }, formatter: (ps) => `${ps[0].name}: <b>${fmt(ps[0].value)}</b>` },
      xAxis: {
        type: "category",
        data: data.history.map((h) => h.month),
        axisLine: { lineStyle: { color: css("--grid") } },
        axisTick: { show: false },
        axisLabel: { color: css("--text-2"), fontSize: 11 },
      },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { color: css("--grid") } },
        axisLabel: { color: css("--text-2"), fontSize: 11, formatter: (v) => (v >= 100000 ? v / 100000 + " mil" : v / 100) },
      },
      series: [
        {
          type: "bar",
          data: data.history.map((h) => h.expense),
          barMaxWidth: 28,
          itemStyle: { color: css("--accent"), borderRadius: [4, 4, 0, 0] },
        },
      ],
    };
  }

  function draw() {
    for (const [id, build] of [["cat-chart", categories], ["hist-chart", history]]) {
      const el = document.getElementById(id);
      if (!el || !window.echarts) continue;
      const chart = echarts.getInstanceByDom(el) || echarts.init(el, null, { renderer: "svg" });
      chart.setOption(build(el), true);
      charts.push(chart);
    }
  }

  draw();
  window.addEventListener("resize", () => charts.forEach((c) => c.resize()));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
})();
