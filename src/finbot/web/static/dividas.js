// Simulador de todas as dívidas: o controle deslizante pede ao servidor
// /api/simular?extra=CENTAVOS (as contas do /estrategia) e redesenha.
(function () {
  "use strict";

  const brl = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
  const fmt = (c) => brl.format(c / 100);
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const $ = (id) => document.getElementById(id);
  const slider = $("extra");
  let chart = null;
  let timer = null;
  let last = null;

  function setText(id, text) { $(id).textContent = text; }

  function endText(r) {
    return r.feasible ? `Tudo quitado em ${r.end}` : "Não termina (os juros comem o pagamento)";
  }

  function series(data) {
    // Total devido: começa hoje (start) e segue a trajetória mês a mês.
    const pad = (t) => {
      const values = [data.start, ...t];
      while (values.length < data.labels.length) values.push(0);
      return values.map((v) => v / 100);
    };
    return { base: pad(data.base.trajectory), ava: pad(data.avalanche.trajectory) };
  }

  function draw(data) {
    if (!window.echarts) return;
    chart = chart || echarts.init($("sim-chart"), null, { renderer: "svg" });
    const s = series(data);
    const extraLabel = data.extra ? `Com +${fmt(data.extra)}/mês` : "Só as parcelas";
    const text = css("--text-2");
    chart.setOption(
      {
        animationDuration: 300,
        grid: { left: 8, right: 12, top: 34, bottom: 4, containLabel: true },
        legend: { top: 0, left: 0, textStyle: { color: text, fontSize: 12 }, itemWidth: 18 },
        tooltip: {
          trigger: "axis",
          confine: true,
          backgroundColor: css("--surface"),
          borderColor: css("--border"),
          textStyle: { color: css("--text"), fontSize: 12 },
          valueFormatter: (v) => brl.format(v),
        },
        xAxis: {
          type: "category",
          data: data.labels,
          boundaryGap: false,
          axisLine: { lineStyle: { color: css("--grid") } },
          axisTick: { show: false },
          axisLabel: { color: text, fontSize: 11, hideOverlap: true },
        },
        yAxis: {
          type: "value",
          splitLine: { lineStyle: { color: css("--grid") } },
          axisLabel: { color: text, fontSize: 11, formatter: (v) => (v >= 1000 ? v / 1000 + " mil" : v) },
        },
        series: [
          {
            name: "Só as parcelas",
            type: "line",
            symbol: "none",
            color: text,
            lineStyle: { width: 2, type: "dashed" },
            data: s.base,
          },
          {
            name: extraLabel,
            type: "line",
            symbol: "none",
            color: css("--accent"),
            lineStyle: { width: 2.5 },
            areaStyle: { opacity: 0.1 },
            data: s.ava,
          },
        ],
      },
      true
    );
  }

  function render(data) {
    if (!data.ready) return;
    last = data;
    setText("ava-end", endText(data.avalanche));
    setText("snow-end", endText(data.snowball));
    setText("ava-interest", data.avalanche.feasible ? `juros: ${fmt(data.avalanche.interest)} · ${fmt(data.avalanche.budget)}/mês` : "");
    setText("snow-interest", data.snowball.feasible ? `juros: ${fmt(data.snowball.interest)} · ${fmt(data.snowball.budget)}/mês` : "");
    let saved = "";
    if (data.extra && data.saved_interest !== undefined) {
      saved = `Com +${fmt(data.extra)}/mês (avalanche): termina ${data.saved_months} mês(es) antes e economiza ${fmt(data.saved_interest)} de juros.`;
    } else if (!data.extra) {
      saved = "Arraste para ver o efeito de pagar mais por mês.";
    }
    setText("sim-saved", saved);
    const order = $("ava-order");
    order.innerHTML = "";
    for (const o of data.avalanche.order) {
      const li = document.createElement("li");
      li.textContent = `${o.name}: quitada em ${o.when} (mês ${o.month})`;
      order.appendChild(li);
    }
    draw(data);
  }

  async function load() {
    const extra = Number(slider.value);
    setText("extra-label", fmt(extra));
    const response = await fetch(`/api/simular?extra=${extra}`, { credentials: "same-origin" });
    if (response.status === 401) { window.location = "/login"; return; }
    render(await response.json());
  }

  slider.addEventListener("input", () => {
    setText("extra-label", fmt(Number(slider.value)));
    clearTimeout(timer);
    timer = setTimeout(load, 150); // espera o dedo parar um instante
  });
  window.addEventListener("resize", () => chart && chart.resize());
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => last && draw(last));
  load();
})();
