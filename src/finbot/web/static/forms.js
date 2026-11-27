// Formulário de registro: mostra "Pago com/Parcelas" só para gasto, e as parcelas
// só quando é cartão. Sem JavaScript o formulário funciona igual (o servidor
// ignora cartão/parcelas numa entrada).
(function () {
  "use strict";
  for (const form of document.querySelectorAll("form.entry-form")) {
    const card = form.querySelector('select[name="cartao"]');
    const parcels = form.querySelector('input[name="parcelas"]').closest(".field");
    const expenseOnly = form.querySelector(".only-expense");
    function update() {
      const isExpense = form.querySelector('input[name="tipo"]:checked').value === "expense";
      expenseOnly.hidden = !isExpense;
      parcels.hidden = !card.value;
    }
    form.addEventListener("change", update);
    update();
  }
})();
