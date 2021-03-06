# -*- coding: utf-8 -*-
###############################################################################
#                                                                             #
# Copyright (C) 2016  Dominic Krimmer                                         #
#                     Luis Alfredo da Silva (luis.adasilvaf@gmail.com)        #
#                                                                             #
# This program is free software: you can redistribute it and/or modify        #
# it under the terms of the GNU Affero General Public License as published by #
# the Free Software Foundation, either version 3 of the License, or           #
# (at your option) any later version.                                         #
#                                                                             #
# This program is distributed in the hope that it will be useful,             #
# but WITHOUT ANY WARRANTY; without even the implied warranty of              #
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the               #
# GNU Affero General Public License for more details.                         #
#                                                                             #
# You should have received a copy of the GNU Affero General Public License    #
# along with this program.  If not, see <http://www.gnu.org/licenses/>.       #
###############################################################################

from openerp import api, fields, models

import pprint
from openerp.exceptions import UserError, ValidationError
from openerp.tools.translate import _
from openerp.tools import float_is_zero, float_compare

import logging
_logger = logging.getLogger(__name__)

class AccountInvoice(models.Model):
    """ This Model calculates and saves withholding tax that apply in
    Colombia"""

    _description = 'Model to create and save withholding taxes'
    _name = 'account.invoice'
    _inherit = 'account.invoice'

    # Define withholding as new tax.
    wh_taxes = fields.Monetary('Withholding Tax', store="True",
                               compute="_compute_amount")

    amount_without_wh_tax = fields.Monetary('Total With Tax', store="True",
                                            compute="_compute_amount")
    date_invoice = fields.Date(required=True)
    # Calculate withholding tax and (new) total amount

    @api.one
    @api.depends('invoice_line_ids.price_subtotal', 'tax_line_ids.amount', 'currency_id', 'company_id')
    def _compute_amount(self):
        """
        This functions computes the withholding tax on the untaxed amount
        @return: void
        """
        # Calling the original calculation
        super(AccountInvoice, self)._compute_amount()
        fp_company = self.env['account.fiscal.position'].search(
            [('id', '=', self.company_id.partner_id.property_account_position_id.id)])
        company_tax_ids = [base_tax.tax_id.id for base_tax in fp_company.tax_ids_invoice]

        if self.fiscal_position_id:
            fp_partner = self.env['account.fiscal.position'].search(
                [('id','=',self.fiscal_position_id.id)])

            partner_tax_ids = [base_tax.tax_id.id for base_tax in fp_partner.tax_ids_invoice]

            self.amount_tax = sum(line.amount for line in self.tax_line_ids if line.tax_id.id not in (partner_tax_ids + company_tax_ids))
            self.wh_taxes = abs(sum(line.amount for line in self.tax_line_ids if line.tax_id.id in partner_tax_ids))
        else:
            self.amount_tax = sum(line.amount for line in self.tax_line_ids if line.tax_id.id not in company_tax_ids)

        self.amount_without_wh_tax = self.amount_untaxed + self.amount_tax
        self.amount_total = self.amount_without_wh_tax - self.wh_taxes
        sign = self.type in ['in_refund', 'out_refund'] and -1 or 1
        self.amount_total_signed = self.amount_total * sign

    @api.one
    @api.depends(
        'state', 'currency_id', 'invoice_line_ids.price_subtotal',
        'move_id.line_ids.amount_residual',
        'move_id.line_ids.currency_id')
    def _compute_residual(self):
        fp_company = self.env['account.fiscal.position'].search(
            [('id', '=', self.company_id.partner_id.property_account_position_id.id)])
        company_tax_ids = [base_tax.tax_id.id for base_tax in fp_company.tax_ids_invoice]

        residual = 0.0
        residual_company_signed = 0.0
        sign = self.type in ['in_refund', 'out_refund'] and -1 or 1
        for line in self.sudo().move_id.line_ids:
            if line.tax_line_id.id not in company_tax_ids:
                if line.account_id.internal_type in ('receivable', 'payable'):
                    residual_company_signed += line.amount_residual
                    if line.currency_id == self.currency_id:
                        residual += line.amount_residual_currency if line.currency_id else line.amount_residual
                    else:
                        from_currency = (line.currency_id and line.currency_id.with_context(date=line.date)) or line.company_id.currency_id.with_context(date=line.date)
                        residual += from_currency.compute(line.amount_residual, self.currency_id)
        self.residual_company_signed = abs(residual_company_signed) * sign
        self.residual_signed = abs(residual) * sign
        self.residual = abs(residual)
        digits_rounding_precision = self.currency_id.rounding
        if float_is_zero(self.residual, precision_rounding=digits_rounding_precision):
            self.reconciled = True
        else:
            self.reconciled = False

    @api.multi
    def _get_tax_amount_by_group(self):
        res = super(AccountInvoice, self)._get_tax_amount_by_group()
        groups_not_in_invoice = self.env['account.tax.group'].search_read([('not_in_invoice','=',True)],['name'])

        for g in groups_not_in_invoice:
            for i in res:
                if g['name'] == i[0]:
                    res.remove(i)

        return res

    def at_least_one_tax_group_enabled(self):
        res = False
        groups = self.env['account.tax'].search_read([('id','in',[invoice_tax.tax_id.id for invoice_tax in self.tax_line_ids])],['tax_group_id'])

        in_invoice = set()
        for group in groups:
            in_invoice.add(group['tax_group_id'][0])
        in_invoice = list(in_invoice)

        dont_show = [i.id for i in self.env['account.tax.group'].search([('not_in_invoice','=',True),
                                                                         ('id','in',in_invoice)])]
        if len(dont_show) < len(in_invoice):
            res = True

        return res

    @api.onchange('payment_term_id', 'date_invoice')
    def _onchange_payment_term_date_invoice(self):
        self.date_invoice = fields.Date.context_today(self)
        res = super(AccountInvoice, self)._onchange_payment_term_date_invoice()
        return res

    @api.onchange('partner_id', 'company_id')
    def _onchange_partner_id(self):
        self.date_invoice = fields.Date.context_today(self)
        res = super(AccountInvoice, self)._onchange_partner_id()
        self._onchange_invoice_line_ids()
        return res

    @api.multi
    def get_taxes_values(self):
        tax_grouped = super(AccountInvoice, self).get_taxes_values()

        if self.company_id.partner_id.property_account_position_id:
            fp = self.env['account.fiscal.position'].search(
                [('id', '=', self.company_id.partner_id.property_account_position_id.id)])
            fp.ensure_one()

            tax_ids = self.env['account.tax'].search([('id', 'in', [tax.tax_id.id for tax in fp.tax_ids_invoice]),
                                                      ('type_tax_use', '=', 'sale')])
            for tax_id in tax_ids:
                tax = tax_id.compute_all(self.amount_untaxed, self.currency_id, partner=self.partner_id)['taxes'][0]
                val = {
                    'invoice_id': self.id,
                    'name': tax['name'],
                    'tax_id': tax['id'],
                    'amount': abs(tax['amount']),
                    'manual': False,
                    'sequence': tax['sequence'],
                    'account_analytic_id': tax['analytic'] or False,
                    'account_id': self.type in 'out_invoice' and tax['account_id'] or tax['refund_account_id'],
                }

                key = self.env['account.tax'].browse(tax['id']).get_grouping_key(val)

                if key not in tax_grouped:
                    tax_grouped[key] = val
                else:
                    tax_grouped[key]['amount'] += val['amount']
        else:
            raise UserError(_('Debe definir una posicion fiscal para el partner asociado a la compañía actual'))

        if self.fiscal_position_id:
            fp = self.env['account.fiscal.position'].search([('id','=',self.fiscal_position_id.id)])
            fp.ensure_one()

            type_tax = 'sale' if self.type in ('out_invoice', 'out_refund') else 'purchase'
            tax_ids = self.env['account.tax'].search([('id','in',[tax.tax_id.id for tax in fp.tax_ids_invoice]),
                                                      ('type_tax_use','=',type_tax),
                                                      ('base_taxes','>',0)])

            tax_ids = [tax.id for tax in tax_ids]
            base_taxes = self.env['account.base.tax'].search([('start_date','<=',self.date_invoice),
                                                              ('end_date','>=',self.date_invoice),
                                                              ('amount', '<=', self.amount_untaxed),
                                                              ('tax_id','in',tax_ids)])

            for base in base_taxes:
                tax = base.tax_id.compute_all(self.amount_untaxed, self.currency_id, partner=self.partner_id)['taxes'][0]
                val = {
                    'invoice_id': self.id,
                    'name': tax['name'],
                    'tax_id': tax['id'],
                    'amount': tax['amount'],
                    'manual': False,
                    'sequence': tax['sequence'],
                    'account_analytic_id': tax['analytic'] or False,
                    'account_id': self.type in ('out_invoice', 'in_invoice') and tax['account_id'] or tax['refund_account_id'],
                }

                key = self.env['account.tax'].browse(tax['id']).get_grouping_key(val)

                if key not in tax_grouped:
                    tax_grouped[key] = val
                else:
                    tax_grouped[key]['amount'] += val['amount']

        return tax_grouped

    @api.model
    def tax_line_move_line_get(self):
        result = super(AccountInvoice, self).tax_line_move_line_get()

        if self.type in 'out_invoice':
            fp = self.env['account.fiscal.position'].search(
                [('id', '=', self.company_id.partner_id.property_account_position_id.id)])
            fp.ensure_one()

            tax_ids = self.env['account.tax'].search([('id', 'in', [tax.tax_id.id for tax in fp.tax_ids_invoice]),
                                                      ('type_tax_use', '=', 'sale')])
            tax_ids = [tax.id for tax in tax_ids]
            for tax_line in sorted(self.tax_line_ids, key=lambda x: -x.sequence):
                if tax_line.tax_id.id in tax_ids:
                    result.append({
                        'invoice_tax_line_id': tax_line.id,
                        'tax_line_id': tax_line.tax_id.id,
                        'type': 'tax',
                        'name': tax_line.name,
                        'price_unit': tax_line.amount,
                        'quantity': 1,
                        'price': tax_line.amount * -1,
                        'account_id': tax_line.tax_id.account_id_counterpart.id,
                        'account_analytic_id': tax_line.account_analytic_id.id,
                        'invoice_id': self.id,
                        'tax_ids': [(6, 0, done_taxes)] if tax_line.tax_id.include_base_amount else []
                    })
        return result

    @api.onchange('fiscal_position_id','date_invoice')
    def _onchange_fiscal_position_id(self):
        if not self.date_invoice:
            self.date_invoice = fields.Date.context_today(self)
        self._onchange_invoice_line_ids()

class AccountInvoiceLine(models.Model):
    _name = 'account.invoice.line'
    _inherit = 'account.invoice.line'

class AccountTax(models.Model):
    _name = 'account.tax'
    _inherit = 'account.tax'

    tax_in_invoice = fields.Boolean(string="Evaluate in invoice", default=False,
        help="Check this if you want to hide the tax from the taxes list in products")
    dont_impact_balance = fields.Boolean(string="Don't impact balance", default=False,
        help="Check this if you want to assign counterpart taxes accounts")
    account_id_counterpart = fields.Many2one('account.account', string='Tax Account Counterpart', ondelete='restrict',
        help="Account that will be set on invoice tax lines for invoices. Leave empty to use the expense account.")
    refund_account_id_counterpart = fields.Many2one('account.account', string='Tax Account Counterpart on Refunds', ondelete='restrict',
        help="Account that will be set on invoice tax lines for refunds. Leave empty to use the expense account.")
    position_id = fields.Many2one('account.fiscal.position', string='Fiscal position related id')
    base_taxes = fields.One2many('account.base.tax', 'tax_id', string='Base taxes', help='This field show related taxes applied to this tax')

    @api.onchange('account_id_counterpart')
    def onchange_account_id_counterpart(self):
        self.refund_account_id_counterpart = self.account_id_counterpart

    @api.v8
    def compute_all(self, price_unit, currency=None, quantity=1.0, product=None, partner=None):
        result = super(AccountTax, self).compute_all(price_unit, currency=currency, quantity=quantity, product=product, partner=partner)
        for tax in self.sorted(key=lambda r: r.sequence):
            for iter_tax in result['taxes']:
                if iter_tax['id'] == tax.id:
                    iter_tax['account_id_counterpart'] = tax.account_id_counterpart.id
                    iter_tax['refund_account_id_counterpart'] = tax.refund_account_id_counterpart.id

        return result


class AccountBaseTax(models.Model):
    _name = 'account.base.tax'

    tax_id = fields.Many2one('account.tax', string='Tax related')
    start_date = fields.Date(string='Since date', required=True)
    end_date = fields.Date(string='Until date', required=True)
    amount = fields.Float(digits=0, default=0, string="Tax amount", required=True)
    # currency_id = fields.Many2one('res.currency', related='tax_id.company_id.currency_id', store=True)

    @api.one
    @api.constrains('start_date', 'end_date')
    def _check_closing_date(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError("Error! End date cannot be set before start date.")

    @api.multi
    @api.constrains('start_date', 'end_date')
    def _dont_overlap_date(self):
        bases_ids = self.search([('start_date', '<=', self.end_date),
                                 ('end_date', '>=', self.start_date),
                                 ('tax_id', '=', self.tax_id.id),
                                 ('id', '<>', self.id)])

        if bases_ids:
            raise ValidationError("Error! cannot have overlap date range.")


class AccountTaxGroup(models.Model):
    _name = 'account.tax.group'
    _inherit = 'account.tax.group'

    not_in_invoice = fields.Boolean(string="Don't show in invoice", default=False,
        help="Check this if you want to hide the taxes in this group when print an invoice")

class AccountFiscalPositionTaxes(models.Model):
    _name = 'account.fiscal.position.base.tax'

    position_id = fields.Many2one('account.fiscal.position', string='Fiscal position related')
    tax_id = fields.Many2one('account.tax', string='Tax')
    amount = fields.Float(related='tax_id.amount', store=True, readonly=True)

    # _sql_constraints = [
    #     ('tax_fiscal_position_uniq', 'unique(position_id, tax_id)', _('Error! cannot have repeated taxes'))
    # ]

    @api.constrains('tax_id')
    def _check_dont_repeat_tax(self):
        local_taxes = self.search([('position_id', '=', self.position_id.id),
                                   ('tax_id', '=', self.tax_id.id),
                                   ('id', '<>', self.id)])

        if local_taxes:
            raise ValidationError("Error! cannot have repeated taxes")

class AccountFiscalPosition(models.Model):
    _name = 'account.fiscal.position'
    _inherit = 'account.fiscal.position'

    tax_ids_invoice = fields.One2many('account.fiscal.position.base.tax', 'position_id',
        string='Taxes that refer to the fiscal position')