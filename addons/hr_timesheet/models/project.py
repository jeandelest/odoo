# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from collections import defaultdict

from odoo import models, fields, api, _
from odoo.tools.float_utils import float_compare
from odoo.exceptions import UserError, ValidationError, RedirectWarning
from odoo.addons.rating.models.rating_data import OPERATOR_MAPPING

PROJECT_TASK_READABLE_FIELDS = {
    'allow_subtasks',
    'allow_timesheets',
    'analytic_account_active',
    'effective_hours',
    'encode_uom_in_days',
    'planned_hours',
    'progress',
    'overtime',
    'remaining_hours',
    'subtask_effective_hours',
    'subtask_planned_hours',
    'timesheet_ids',
    'total_hours_spent',
}

class Project(models.Model):
    _inherit = "project.project"

    allow_timesheets = fields.Boolean(
        "Timesheets", compute='_compute_allow_timesheets', store=True, readonly=False,
        default=True)
    analytic_account_id = fields.Many2one(
        # note: replaces ['|', ('company_id', '=', False), ('company_id', '=', company_id)]
        domain="""[
            '|', ('company_id', '=', False), ('company_id', '=', company_id),
            ('partner_id', '=?', partner_id),
        ]"""
    )

    timesheet_ids = fields.One2many('account.analytic.line', 'project_id', 'Associated Timesheets')
    timesheet_count = fields.Integer(compute="_compute_timesheet_count", groups='hr_timesheet.group_hr_timesheet_user')
    timesheet_encode_uom_id = fields.Many2one('uom.uom', related='company_id.timesheet_encode_uom_id')
    total_timesheet_time = fields.Integer(
        compute='_compute_total_timesheet_time', groups='hr_timesheet.group_hr_timesheet_user',
        help="Total number of time (in the proper UoM) recorded in the project, rounded to the unit.")
    encode_uom_in_days = fields.Boolean(compute='_compute_encode_uom_in_days')
    is_internal_project = fields.Boolean(compute='_compute_is_internal_project', search='_search_is_internal_project')
    remaining_hours = fields.Float(compute='_compute_remaining_hours', string='Remaining Invoiced Time', compute_sudo=True)
    is_project_overtime = fields.Boolean('Project in Overtime', compute='_compute_remaining_hours', search='_search_is_project_overtime', compute_sudo=True)
    allocated_hours = fields.Float(string='Allocated Hours')

    def _compute_encode_uom_in_days(self):
        self.encode_uom_in_days = self.env.company.timesheet_encode_uom_id == self.env.ref('uom.product_uom_day')

    @api.depends('analytic_account_id')
    def _compute_allow_timesheets(self):
        without_account = self.filtered(lambda t: not t.analytic_account_id and t._origin)
        without_account.update({'allow_timesheets': False})

    @api.depends('company_id')
    def _compute_is_internal_project(self):
        for project in self:
            project.is_internal_project = project == project.company_id.internal_project_id

    @api.model
    def _search_is_internal_project(self, operator, value):
        if not isinstance(value, bool):
            raise ValueError(_('Invalid value: %s', value))
        if operator not in ['=', '!=']:
            raise ValueError(_('Invalid operator: %s', operator))

        query = """
            SELECT C.internal_project_id
            FROM res_company C
            WHERE C.internal_project_id IS NOT NULL
        """
        if (operator == '=' and value is True) or (operator == '!=' and value is False):
            operator_new = 'inselect'
        else:
            operator_new = 'not inselect'
        return [('id', operator_new, (query, ()))]

    @api.model
    def _get_view_cache_key(self, view_id=None, view_type='form', **options):
        """The override of _get_view changing the time field labels according to the company timesheet encoding UOM
        makes the view cache dependent on the company timesheet encoding uom"""
        key = super()._get_view_cache_key(view_id, view_type, **options)
        return key + (self.env.company.timesheet_encode_uom_id,)

    @api.model
    def _get_view(self, view_id=None, view_type='form', **options):
        arch, view = super()._get_view(view_id, view_type, **options)
        if view_type in ['tree', 'form'] and self.env.company.timesheet_encode_uom_id == self.env.ref('uom.product_uom_day'):
            arch = self.env['account.analytic.line']._apply_time_label(arch, related_model=self._name)
        return arch, view

    @api.depends('allow_timesheets', 'timesheet_ids')
    def _compute_remaining_hours(self):
        timesheets_read_group = self.env['account.analytic.line']._read_group(
            [('project_id', 'in', self.ids)],
            ['project_id', 'unit_amount'],
            ['project_id'],
            lazy=False)
        timesheet_time_dict = {res['project_id'][0]: res['unit_amount'] for res in timesheets_read_group}
        for project in self:
            project.remaining_hours = project.allocated_hours - timesheet_time_dict.get(project.id, 0)
            project.is_project_overtime = project.remaining_hours < 0

    @api.model
    def _search_is_project_overtime(self, operator, value):
        if not isinstance(value, bool):
            raise ValueError(_('Invalid value: %s') % value)
        if operator not in ['=', '!=']:
            raise ValueError(_('Invalid operator: %s') % operator)

        query = """
            SELECT Project.id
              FROM project_project AS Project
              JOIN project_task AS Task
                ON Project.id = Task.project_id
             WHERE Project.allocated_hours > 0
               AND Project.allow_timesheets = TRUE
               AND Task.parent_id IS NULL
               AND Task.is_closed IS FALSE
          GROUP BY Project.id
            HAVING Project.allocated_hours - SUM(Task.effective_hours) < 0
        """
        if (operator == '=' and value is True) or (operator == '!=' and value is False):
            operator_new = 'inselect'
        else:
            operator_new = 'not inselect'
        return [('id', operator_new, (query, ()))]

    @api.constrains('allow_timesheets', 'analytic_account_id')
    def _check_allow_timesheet(self):
        for project in self:
            if project.allow_timesheets and not project.analytic_account_id:
                raise ValidationError(_('You cannot use timesheets without an analytic account.'))

    @api.depends('timesheet_ids')
    def _compute_total_timesheet_time(self):
        timesheets_read_group = self.env['account.analytic.line'].read_group(
            [('project_id', 'in', self.ids)],
            ['project_id', 'unit_amount', 'product_uom_id'],
            ['project_id', 'product_uom_id'],
            lazy=False)
        timesheet_time_dict = defaultdict(list)
        uom_ids = set(self.timesheet_encode_uom_id.ids)

        for result in timesheets_read_group:
            uom_id = result['product_uom_id'] and result['product_uom_id'][0]
            if uom_id:
                uom_ids.add(uom_id)
            timesheet_time_dict[result['project_id'][0]].append((uom_id, result['unit_amount']))

        uoms_dict = {uom.id: uom for uom in self.env['uom.uom'].browse(uom_ids)}
        for project in self:
            # Timesheets may be stored in a different unit of measure, so first
            # we convert all of them to the reference unit
            # if the timesheet has no product_uom_id then we take the one of the project
            total_time = 0.0
            for product_uom_id, unit_amount in timesheet_time_dict[project.id]:
                factor = uoms_dict.get(product_uom_id, project.timesheet_encode_uom_id).factor_inv
                total_time += unit_amount * (1.0 if project.encode_uom_in_days else factor)
            # Now convert to the proper unit of measure set in the settings
            total_time *= project.timesheet_encode_uom_id.factor
            project.total_timesheet_time = int(round(total_time))

    @api.depends('timesheet_ids')
    def _compute_timesheet_count(self):
        timesheet_read_group = self.env['account.analytic.line']._read_group(
            [('project_id', 'in', self.ids)],
            ['project_id'],
            ['project_id']
        )
        timesheet_project_map = {project_info['project_id'][0]: project_info['project_id_count'] for project_info in timesheet_read_group}
        for project in self:
            project.timesheet_count = timesheet_project_map.get(project.id, 0)

    @api.model_create_multi
    def create(self, vals_list):
        """ Create an analytic account if project allow timesheet and don't provide one
            Note: create it before calling super() to avoid raising the ValidationError from _check_allow_timesheet
        """
        defaults = self.default_get(['allow_timesheets', 'analytic_account_id'])
        for vals in vals_list:
            allow_timesheets = vals.get('allow_timesheets', defaults.get('allow_timesheets'))
            analytic_account_id = vals.get('analytic_account_id', defaults.get('analytic_account_id'))
            if allow_timesheets and not analytic_account_id:
                analytic_account = self._create_analytic_account_from_values(vals)
                vals['analytic_account_id'] = analytic_account.id
        return super().create(vals_list)

    def write(self, values):
        # create the AA for project still allowing timesheet
        if values.get('allow_timesheets') and not values.get('analytic_account_id'):
            for project in self:
                if not project.analytic_account_id:
                    project._create_analytic_account()
        return super(Project, self).write(values)

    def name_get(self):
        res = super().name_get()
        if len(self.env.context.get('allowed_company_ids', [])) <= 1:
            return res
        name_mapping = dict(res)
        for project in self:
            if project.is_internal_project:
                name_mapping[project.id] = f'{name_mapping[project.id]} - {project.company_id.name}'
        return list(name_mapping.items())

    @api.model
    def _init_data_analytic_account(self):
        self.search([('analytic_account_id', '=', False), ('allow_timesheets', '=', True)])._create_analytic_account()

    @api.ondelete(at_uninstall=False)
    def _unlink_except_contains_entries(self):
        """
        If some projects to unlink have some timesheets entries, these
        timesheets entries must be unlinked first.
        In this case, a warning message is displayed through a RedirectWarning
        and allows the user to see timesheets entries to unlink.
        """
        projects_with_timesheets = self.filtered(lambda p: p.timesheet_ids)
        if projects_with_timesheets:
            if len(projects_with_timesheets) > 1:
                warning_msg = _("These projects have some timesheet entries referencing them. Before removing these projects, you have to remove these timesheet entries.")
            else:
                warning_msg = _("This project has some timesheet entries referencing it. Before removing this project, you have to remove these timesheet entries.")
            raise RedirectWarning(
                warning_msg, self.env.ref('hr_timesheet.timesheet_action_project').id,
                _('See timesheet entries'), {'active_ids': projects_with_timesheets.ids})

    def _convert_project_uom_to_timesheet_encode_uom(self, time):
        uom_from = self.company_id.project_time_mode_id
        uom_to = self.env.company.timesheet_encode_uom_id
        return round(uom_from._compute_quantity(time, uom_to, raise_if_failure=False), 2)

    def action_project_timesheets(self):
        action = self.env['ir.actions.act_window']._for_xml_id('hr_timesheet.act_hr_timesheet_line_by_project')
        action['display_name'] = _("%(name)s's Timesheets", name=self.name)
        return action


class Task(models.Model):
    _name = "project.task"
    _inherit = "project.task"

    project_id = fields.Many2one(domain=[('is_internal_project', '=', False)])
    display_project_id = fields.Many2one(domain=[('is_internal_project', '=', False)])
    analytic_account_active = fields.Boolean("Active Analytic Account", compute='_compute_analytic_account_active', compute_sudo=True)
    allow_timesheets = fields.Boolean("Allow timesheets", related='project_id.allow_timesheets', help="Timesheets can be logged on this task.", readonly=True)
    remaining_hours = fields.Float("Remaining Hours", compute='_compute_remaining_hours', store=True, readonly=True, help="Number of allocated hours minus the number of hours spent.")
    remaining_hours_percentage = fields.Float(compute='_compute_remaining_hours_percentage', search='_search_remaining_hours_percentage')
    effective_hours = fields.Float("Hours Spent", compute='_compute_effective_hours', compute_sudo=True, store=True)
    total_hours_spent = fields.Float("Total Hours", compute='_compute_total_hours_spent', store=True, help="Time spent on this task and its sub-tasks (and their own sub-tasks).")
    progress = fields.Float("Progress", compute='_compute_progress_hours', store=True, group_operator="avg")
    overtime = fields.Float(compute='_compute_progress_hours', store=True)
    subtask_effective_hours = fields.Float("Hours Spent on Sub-Tasks", compute='_compute_subtask_effective_hours', recursive=True, store=True, help="Time spent on the sub-tasks (and their own sub-tasks) of this task.")
    timesheet_ids = fields.One2many('account.analytic.line', 'task_id', 'Timesheets')
    encode_uom_in_days = fields.Boolean(compute='_compute_encode_uom_in_days', default=lambda self: self._uom_in_days())

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS | PROJECT_TASK_READABLE_FIELDS

    def _uom_in_days(self):
        return self.env.company.timesheet_encode_uom_id == self.env.ref('uom.product_uom_day')

    def _compute_encode_uom_in_days(self):
        self.encode_uom_in_days = self._uom_in_days()

    @api.depends('analytic_account_id.active', 'project_id.analytic_account_id.active')
    def _compute_analytic_account_active(self):
        """ Overridden in sale_timesheet """
        for task in self:
            task.analytic_account_active = task._get_task_analytic_account_id().active

    @api.depends('timesheet_ids.unit_amount')
    def _compute_effective_hours(self):
        if not any(self._ids):
            for task in self:
                task.effective_hours = sum(task.timesheet_ids.mapped('unit_amount'))
            return
        timesheet_read_group = self.env['account.analytic.line'].read_group([('task_id', 'in', self.ids)], ['unit_amount', 'task_id'], ['task_id'])
        timesheets_per_task = {res['task_id'][0]: res['unit_amount'] for res in timesheet_read_group}
        for task in self:
            task.effective_hours = timesheets_per_task.get(task.id, 0.0)

    @api.depends('effective_hours', 'subtask_effective_hours', 'planned_hours')
    def _compute_progress_hours(self):
        for task in self:
            if (task.planned_hours > 0.0):
                task_total_hours = task.effective_hours + task.subtask_effective_hours
                task.overtime = max(task_total_hours - task.planned_hours, 0)
                if float_compare(task_total_hours, task.planned_hours, precision_digits=2) >= 0:
                    task.progress = 100
                else:
                    task.progress = round(100.0 * task_total_hours / task.planned_hours, 2)
            else:
                task.progress = 0.0
                task.overtime = 0

    @api.depends('planned_hours', 'remaining_hours')
    def _compute_remaining_hours_percentage(self):
        for task in self:
            if task.planned_hours > 0.0:
                task.remaining_hours_percentage = task.remaining_hours / task.planned_hours
            else:
                task.remaining_hours_percentage = 0.0

    def _search_remaining_hours_percentage(self, operator, value):
        if operator not in OPERATOR_MAPPING:
            raise NotImplementedError(_('This operator %s is not supported in this search method.', operator))
        query = f"""
            SELECT id
              FROM {self._table}
             WHERE remaining_hours > 0
               AND planned_hours > 0
               AND remaining_hours / planned_hours {operator} %s
            """
        return [('id', 'inselect', (query, (value,)))]

    @api.depends('effective_hours', 'subtask_effective_hours', 'planned_hours')
    def _compute_remaining_hours(self):
        for task in self:
            task.remaining_hours = task.planned_hours - task.effective_hours - task.subtask_effective_hours

    @api.depends('effective_hours', 'subtask_effective_hours')
    def _compute_total_hours_spent(self):
        for task in self:
            task.total_hours_spent = task.effective_hours + task.subtask_effective_hours

    @api.depends('child_ids.effective_hours', 'child_ids.subtask_effective_hours')
    def _compute_subtask_effective_hours(self):
        for task in self.with_context(active_test=False):
            task.subtask_effective_hours = sum(child_task.effective_hours + child_task.subtask_effective_hours for child_task in task.child_ids)

    def action_view_subtask_timesheet(self):
        self.ensure_one()
        task_ids = self.with_context(active_test=False)._get_subtask_ids_per_task_id().get(self.id, [])
        action = self.env["ir.actions.actions"]._for_xml_id("hr_timesheet.timesheet_action_all")
        graph_view_id = self.env.ref("hr_timesheet.view_hr_timesheet_line_graph_by_employee").id
        new_views = []
        for view in action['views']:
            if view[1] == 'graph':
                view = (graph_view_id, 'graph')
            new_views.insert(0, view) if view[1] == 'tree' else new_views.append(view)
        action.update({
            'display_name': _('Timesheets'),
            'context': {'default_project_id': self.project_id.id, 'grid_range': 'week'},
            'domain': [('project_id', '!=', False), ('task_id', 'in', task_ids)],
            'views': new_views,
        })
        return action

    def _get_timesheet(self):
        # Is override in sale_timesheet
        return self.timesheet_ids

    def write(self, values):
        # a timesheet must have an analytic account (and a project)
        if 'project_id' in values and not values.get('project_id') and self._get_timesheet():
            raise UserError(_('This task must be part of a project because there are some timesheets linked to it.'))
        res = super(Task, self).write(values)

        if 'project_id' in values:
            project = self.env['project.project'].browse(values.get('project_id'))
            if project.allow_timesheets:
                # We write on all non yet invoiced timesheet the new project_id (if project allow timesheet)
                self._get_timesheet().write({'project_id': values.get('project_id')})

        return res

    def name_get(self):
        if self.env.context.get('hr_timesheet_display_remaining_hours'):
            name_mapping = dict(super().name_get())
            for task in self:
                if task.allow_timesheets and task.planned_hours > 0 and task.encode_uom_in_days:
                    days_left = _("(%s days remaining)") % task._convert_hours_to_days(task.remaining_hours)
                    name_mapping[task.id] = name_mapping.get(task.id, '') + u"\u00A0" + days_left
                elif task.allow_timesheets and task.planned_hours > 0:
                    hours, mins = (str(int(duration)).rjust(2, '0') for duration in divmod(abs(task.remaining_hours) * 60, 60))
                    hours_left = _(
                        "(%(sign)s%(hours)s:%(minutes)s remaining)",
                        sign='-' if task.remaining_hours < 0 else '',
                        hours=hours,
                        minutes=mins,
                    )
                    name_mapping[task.id] = name_mapping.get(task.id, '') + u"\u00A0" + hours_left
            return list(name_mapping.items())
        return super().name_get()

    @api.model
    def _get_view_cache_key(self, view_id=None, view_type='form', **options):
        """The override of _get_view changing the time field labels according to the company timesheet encoding UOM
        makes the view cache dependent on the company timesheet encoding uom"""
        key = super()._get_view_cache_key(view_id, view_type, **options)
        return key + (self.env.company.timesheet_encode_uom_id,)

    @api.model
    def _get_view(self, view_id=None, view_type='form', **options):
        """ Set the correct label for `unit_amount`, depending on company UoM """
        arch, view = super()._get_view(view_id, view_type, **options)
        # Use of sudo as the portal user doesn't have access to uom
        arch = self.env['account.analytic.line'].sudo()._apply_timesheet_label(arch)

        if view_type in ['tree', 'pivot', 'graph', 'form'] and self.env.company.timesheet_encode_uom_id == self.env.ref('uom.product_uom_day'):
            arch = self.env['account.analytic.line']._apply_time_label(arch, related_model=self._name)

        return arch, view

    @api.ondelete(at_uninstall=False)
    def _unlink_except_contains_entries(self):
        """
        If some tasks to unlink have some timesheets entries, these
        timesheets entries must be unlinked first.
        In this case, a warning message is displayed through a RedirectWarning
        and allows the user to see timesheets entries to unlink.
        """
        timesheet_data = self.env['account.analytic.line'].sudo()._read_group(
            [('task_id', 'in', self.ids)],
            ['task_id'],
            ['task_id'],
        )
        task_with_timesheets_ids = [res['task_id'][0] for res in timesheet_data]
        if task_with_timesheets_ids:
            if len(task_with_timesheets_ids) > 1:
                warning_msg = _("These tasks have some timesheet entries referencing them. Before removing these tasks, you have to remove these timesheet entries.")
            else:
                warning_msg = _("This task has some timesheet entries referencing it. Before removing this task, you have to remove these timesheet entries.")
            raise RedirectWarning(
                warning_msg, self.env.ref('hr_timesheet.timesheet_action_task').id,
                _('See timesheet entries'), {'active_ids': task_with_timesheets_ids})

    @api.model
    def _convert_hours_to_days(self, time):
        uom_hour = self.env.ref('uom.product_uom_hour')
        uom_day = self.env.ref('uom.product_uom_day')
        return round(uom_hour._compute_quantity(time, uom_day, raise_if_failure=False), 2)
