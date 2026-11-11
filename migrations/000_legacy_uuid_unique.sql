-- F1: a 001 faz REFERENCES empresa(uuid) e usuariomei(uuid), mas o DDL legado do
-- main.py não cria PK/UNIQUE nessas colunas. Esta migration só ACRESCENTA um
-- índice único (nada é alterado ou removido) e, antes, confere o banco:
--   * coluna uuid ausente ou com tipo diferente de texto -> para com erro claro;
--   * uuid duplicado -> para com erro claro (o índice não é criado);
--   * já existe índice único só em uuid (PK, UNIQUE ou índice) -> não faz nada.
-- Roda numa transação do migrate.py: qualquer erro desfaz tudo.

DO $$
DECLARE
    target TEXT;
    col_type TEXT;
    v_attnum SMALLINT;
    duplicated BIGINT;
BEGIN
    FOREACH target IN ARRAY ARRAY['empresa', 'usuariomei'] LOOP
        SELECT a.attnum, format_type(a.atttypid, a.atttypmod)
          INTO v_attnum, col_type
          FROM pg_attribute a
         WHERE a.attrelid = to_regclass(target)
           AND a.attname = 'uuid'
           AND NOT a.attisdropped;
        IF v_attnum IS NULL THEN
            RAISE EXCEPTION 'F1: tabela % sem coluna uuid; migration interrompida', target;
        END IF;
        IF col_type NOT IN ('text', 'character varying')
           AND col_type NOT LIKE 'character varying(%' THEN
            RAISE EXCEPTION 'F1: %.uuid tem tipo % (esperado texto); migration interrompida',
                target, col_type;
        END IF;

        IF EXISTS (
            SELECT 1 FROM pg_index i
             WHERE i.indrelid = to_regclass(target)
               AND i.indisunique AND i.indpred IS NULL
               AND i.indnkeyatts = 1 AND i.indkey[0] = v_attnum
        ) THEN
            CONTINUE;
        END IF;

        EXECUTE format(
            'SELECT count(*) FROM (SELECT uuid FROM %I WHERE uuid IS NOT NULL '
            'GROUP BY uuid HAVING count(*) > 1) d', target
        ) INTO duplicated;
        IF duplicated > 0 THEN
            RAISE EXCEPTION 'F1: % uuid(s) duplicado(s) em %; migration interrompida',
                duplicated, target;
        END IF;

        EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I (uuid)',
            target || '_uuid_key', target);
    END LOOP;
END $$;
