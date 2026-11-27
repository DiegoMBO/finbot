-- Migração 006: desconto em folha com mês de início.
-- Só ACRESCENTA uma coluna; não altera nem apaga dados.
-- Consignado: o 1º desconto costuma vir na folha do mês ANTERIOR ao 1º vencimento
-- (e o 1º vencimento, ~60 dias depois da contratação). Folhas antes desse mês
-- não têm o desconto. NULL = vale para todas as folhas (o comportamento de antes).

ALTER TABLE salary_deductions ADD COLUMN first_competence TEXT;  -- AAAA-MM
