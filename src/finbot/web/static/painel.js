// Gráfico "Saldo dia a dia" do Painel (ECharts).
// Os números vêm de /api/saldo-diario (as mesmas contas do /extrato do bot).
// Uma série só: linha em degraus (o saldo muda no dia de cada movimento), com a
// área abaixo de zero em vermelho. Toque/passe o mouse: o saldo e o que acontece no dia.

(function () {
  "use strict";

  const brl = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
  const fmt = (cents) => brl.format(cents / 100).replace("-", "−");
  const dm = (iso) => iso.slice(8, 10) + "/" + iso.slice(5, 7);

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function tooltip(day) {
    let html = `<b>${dm(day.date)}</b> · saldo <b>${fmt(day.cents)}</b>`;
    for (const it of day.items) {
      const sign = it.cents > 0 ? "+" : "";
      html += `<br>${escapeHtml(it.status)} ${escapeHtml(it.label)}: ${sign}${fmt(it.cents)}`;
    }
    return html;
  }

  function fillTable(days) {
    const body = document.querySelector("#chart-table tbody");
    if (!body) return;
    body.innerHTML = "";
    for (const d of days) {
      if (!d.items.length) continue; // só os dias em que algo acontece
      const tr = document.createElement("tr");
      const what = d.items.map((i) => `${i.label} ${i.cents > 0 ? "+" : ""}${fmt(i.cents)}`).join("; ");
      tr.innerHTML = `<td>${dm(d.date)}</td><td></td><td class="num ${d.cents < 0 ? "neg" : ""}">${fmt(d.cents)}</td>`;
      tr.children[1].textContent = what;
      body.appendChild(tr);
    }
  }

  // [[início, fim], ...] dos períodos com saldo negativo, no formato do markArea.
  function negativeRanges(days) {
    const ranges = [];
    let start = null;
    days.forEach((d, i) => {
      if (d.cents < 0 && start === null) start = i;
      const ends = start !== null && (i === days.length - 1 || days[i + 1].cents >= 0);
      if (ends) {
        ranges.push([{ xAxis: dm(days[start].date) }, { xAxis: dm(days[Math.min(i + 1, days.length - 1)].date) }]);
        start = null;
      }
    });
    return ranges;
  }

  // Só os dias negativos (e o dia em que volta, para o degrau fechar); o resto é null.
  function negativeSegments(days) {
    return days.map((d, i) => {
      const prevNegative = i > 0 && days[i - 1].cents < 0;
      return d.cents < 0 || prevNegative ? d.cents / 100 : null;
    });
  }

  function render(chart, days) {
    const text = css("--text-2");
    const grid = css("--grid");
    const accent = css("--accent");
    const critical = css("--critical");
    chart.setOption(
      {
        animationDuration: 400,
        grid: { left: 8, right: 12, top: 16, bottom: 28, containLabel: true },
        tooltip: {
          trigger: "axis",
          confine: true,
          axisPointer: { type: "line", lineStyle: { color: text, width: 1 } },
          backgroundColor: css("--surface"),
          borderColor: css("--border"),
          textStyle: { color: css("--text"), fontSize: 12 },
          formatter: (params) => tooltip(days[params[0].dataIndex]),
          // o tooltip mostra os itens do dia; as duas séries são o mesmo saldo
        },
        xAxis: {
          type: "category",
          data: days.map((d) => dm(d.date)),
          boundaryGap: false,
          axisLine: { lineStyle: { color: grid } },
          axisTick: { show: false },
          axisLabel: { color: text, fontSize: 11, hideOverlap: true },
        },
        yAxis: {
          type: "value",
          splitLine: { lineStyle: { color: grid } },
          axisLabel: {
            color: text,
            fontSize: 11,
            formatter: (v) => (Math.abs(v) >= 1000 ? (v / 1000).toLocaleString("pt-BR") + " mil" : v),
          },
        },
        dataZoom: [{ type: "inside", minValueSpan: 7 }],
        series: [
          {
            // O saldo de cada dia (azul)
            name: "Saldo",
            type: "line",
            step: "end",
            symbol: "none",
            color: accent,
            lineStyle: { width: 2 },
            areaStyle: { opacity: 0.1 },
            data: days.map((d) => d.cents / 100),
            markLine: {
              silent: true,
              symbol: "none",
              label: { show: false },
              lineStyle: { color: text, type: "dashed", width: 1 },
              data: [{ yAxis: 0 }],
            },
            // Faixas vermelhas nos dias em que a conta fica negativa
            markArea: {
              silent: true,
              itemStyle: { color: critical, opacity: 0.08 },
              data: negativeRanges(days),
            },
          },
          {
            // Por cima: o trecho negativo em vermelho (null = sem linha nos outros dias)
            name: "Negativo",
            type: "line",
            step: "end",
            symbol: "none",
            color: critical,
            lineStyle: { width: 2.5 },
            tooltip: { show: false },
            data: negativeSegments(days),
          },
        ],
      },
      true
    );
  }

  async function main() {
    const el = document.getElementById("chart");
    if (!el || !window.echarts) return;
    const response = await fetch("/api/saldo-diario", { credentials: "same-origin" });
    if (response.status === 401) {
      window.location = "/login";
      return;
    }
    const data = await response.json();
    if (!data.ready) return;
    const chart = echarts.init(el, null, { renderer: "svg" });
    render(chart, data.days);
    fillTable(data.days);
    window.addEventListener("resize", () => chart.resize());
    // Tema do aparelho mudou (claro/escuro): redesenha com as cores novas.
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => render(chart, data.days));
  }

  main();
})();
