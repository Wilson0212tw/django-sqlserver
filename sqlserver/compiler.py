from __future__ import absolute_import, unicode_literals
from django.db.transaction import TransactionManagementError
import django.db.models.sql.compiler
import sqlserver_ado.compiler


# re-export compiler classes from django-mssql
SQLCompiler = sqlserver_ado.compiler.SQLCompiler
SQLInsertCompiler = sqlserver_ado.compiler.SQLInsertCompiler
SQLDeleteCompiler = sqlserver_ado.compiler.SQLDeleteCompiler
SQLUpdateCompiler = sqlserver_ado.compiler.SQLUpdateCompiler
SQLAggregateCompiler = sqlserver_ado.compiler.SQLAggregateCompiler
try:
    SQLDateCompiler = sqlserver_ado.compiler.SQLDateCompiler
except AttributeError:
    pass

try:
    SQLDateTimeCompiler = sqlserver_ado.compiler.SQLDateTimeCompiler
except AttributeError:
    pass


if django.VERSION < (1, 11, 0):
    if django.VERSION >= (1, 9, 0):
        def _get_where(compiler):
            return compiler.where

        def _get_having(compiler):
            return compiler.having
    else:
        def _get_where(compiler):
            return compiler.query.where

        def _get_having(compiler):
            return compiler.query.having

    # monkey-patch django as_sql method
    # can be removed after Django 1.10 is deprecated
    # this was merged into Django 1.11 in https://github.com/django/django/commit/bae64dd0f13ba247448197ecf83cdc7a80691bb4
    def _as_sql(self, with_limits=True, with_col_aliases=False, subquery=False):
        """
        Creates the SQL for this query. Returns the SQL string and list of
        parameters.

        If 'with_limits' is False, any limit/offset information is not included
        in the query.
        """
        # After executing the query, we must get rid of any joins the query
        # setup created. So, take note of alias counts before the query ran.
        # However we do not want to get rid of stuff done in pre_sql_setup(),
        # as the pre_sql_setup will modify query state in a way that forbids
        # another run of it.
        self.subquery = subquery
        refcounts_before = self.query.alias_refcount.copy()
        try:
            extra_select, order_by, group_by = self.pre_sql_setup()
            if with_limits and self.query.low_mark == self.query.high_mark:
                return '', ()
            distinct_fields = self.get_distinct()

            # This must come after 'select', 'ordering', and 'distinct' -- see
            # docstring of get_from_clause() for details.
            from_, f_params = self.get_from_clause()

            where, w_params = self.compile(_get_where(self)) if _get_where(self) is not None else ("", [])
            having, h_params = self.compile(_get_having(self)) if _get_having(self) is not None else ("", [])
            params = []
            result = ['SELECT']

            if self.query.distinct:
                result.append(self.connection.ops.distinct_sql(distinct_fields))

            out_cols = []
            col_idx = 1
            for _, (s_sql, s_params), alias in self.select + extra_select:
                if alias:
                    s_sql = '%s AS %s' % (s_sql, self.connection.ops.quote_name(alias))
                elif with_col_aliases:
                    s_sql = '%s AS %s' % (s_sql, 'Col%d' % col_idx)
                    col_idx += 1
                params.extend(s_params)
                out_cols.append(s_sql)

            result.append(', '.join(out_cols))

            result.append('FROM')
            result.extend(from_)
            params.extend(f_params)

            for_update_part = None
            if self.query.select_for_update and self.connection.features.has_select_for_update:
                if self.connection.get_autocommit():
                    raise TransactionManagementError(
                        "select_for_update cannot be used outside of a transaction."
                    )

                # If we've been asked for a NOWAIT query but the backend does
                # not support it, raise a DatabaseError otherwise we could get
                # an unexpected deadlock.
                nowait = self.query.select_for_update_nowait
                if nowait and not self.connection.features.has_select_for_update_nowait:
                    raise DatabaseError('NOWAIT is not supported on this database backend.')
                for_update_part = self.connection.ops.for_update_sql(nowait=nowait)

            if for_update_part and self.connection.features.for_update_after_from:
                result.append(for_update_part)

            if where:
                result.append('WHERE %s' % where)
                params.extend(w_params)

            grouping = []
            for g_sql, g_params in group_by:
                grouping.append(g_sql)
                params.extend(g_params)
            if grouping:
                if distinct_fields:
                    raise NotImplementedError(
                        "annotate() + distinct(fields) is not implemented.")
                if not order_by:
                    order_by = self.connection.ops.force_no_ordering()
                result.append('GROUP BY %s' % ', '.join(grouping))

            if having:
                result.append('HAVING %s' % having)
                params.extend(h_params)

            if order_by:
                ordering = []
                for _, (o_sql, o_params, _) in order_by:
                    ordering.append(o_sql)
                    params.extend(o_params)
                result.append('ORDER BY %s' % ', '.join(ordering))

            if with_limits:
                if self.query.high_mark is not None:
                    result.append('LIMIT %d' % (self.query.high_mark - self.query.low_mark))
                if self.query.low_mark:
                    if self.query.high_mark is None:
                        val = self.connection.ops.no_limit_value()
                        if val:
                            result.append('LIMIT %d' % val)
                    result.append('OFFSET %d' % self.query.low_mark)

            if for_update_part and not self.connection.features.for_update_after_from:
                result.append(for_update_part)

            q = ' '.join(result)
            # un-escape %% when no parameters passed
            if not params:
                q = q % ()
            return q, tuple(params)
        finally:
            # Finally do cleanup - get rid of the joins we created above.
            self.query.reset_refcounts(refcounts_before)


    django.db.models.sql.compiler.SQLCompiler.as_sql = _as_sql

else:
    # django 1.11 or newer

    # monkey patch django-mssql to support Django 1.11
    # can be removed once django-mssql begins to support Django 1.11
    def _django_11_mssql_monkeypatch_as_sql(self, with_limits=True, with_col_aliases=False):
        # Get out of the way if we're not a select query or there's no limiting involved.
        has_limit_offset = with_limits and (self.query.low_mark or self.query.high_mark is not None)
        try:
            if not has_limit_offset:
                # The ORDER BY clause is invalid in views, inline functions,
                # derived tables, subqueries, and common table expressions,
                # unless TOP or FOR XML is also specified.
                setattr(self.query, '_mssql_ordering_not_allowed', with_col_aliases)

            # let the base do its thing, but we'll handle limit/offset
            sql, fields = super(SQLCompiler, self).as_sql(
                with_limits=False,
                with_col_aliases=with_col_aliases,
            )

            if has_limit_offset:
                if ' order by ' not in sql.lower():
                    # Must have an ORDER BY to slice using OFFSET/FETCH. If
                    # there is none, use the first column, which is typically a
                    # PK
                    sql += ' ORDER BY 1'
                sql += ' OFFSET %d ROWS' % (self.query.low_mark or 0)
                if self.query.high_mark is not None:
                    sql += ' FETCH NEXT %d ROWS ONLY' % (self.query.high_mark - self.query.low_mark)
        finally:
            if not has_limit_offset:
                # remove in case query is ever reused
                delattr(self.query, '_mssql_ordering_not_allowed')

        return sql, fields


    sqlserver_ado.compiler.SQLCompiler.as_sql = _django_11_mssql_monkeypatch_as_sql
