"""
Copyright 2019 Goldman Sachs.
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""

from typing import Union, Iterable
import datetime

from gs_quant.backtests.backtest_utils import *
from gs_quant.backtests.generic_engine import BackTest
from gs_quant.base import Priceable
from gs_quant.markets import HistoricalPricingContext, PricingContext
from gs_quant.markets.portfolio import Portfolio

action_count = 1


class ActionRequirements(object):
    def __init__(self):
        pass


class HedgeActionRequirements(ActionRequirements):
    def __init__(self, start_date=None, end_date=None, frequency=None, calendar=None):
        super().__init__()
        self.start_date = start_date
        self.end_date = end_date
        self.frequency = frequency
        self.calendar = calendar


class Action(object):
    def __init__(self, name: str = None):
        self._needs_scaling = False
        self._deterministic = True
        self._risk = None
        global action_count
        if name is None:
            self._name = 'Action{}'.format(action_count)
            action_count += 1
        else:
            self._name = name

    def apply_action(self, state: Union[datetime.date, Iterable[datetime.date]], backtest: BackTest):
        """
        Used by the Generic Engine
        :param backtest:
        :param state:
        :return:
        """
        raise RuntimeError('apply_action must be implemented by subclass')

    @property
    def deterministic(self):
        return self._deterministic

    @property
    def risk(self):
        return self._risk


class AddTradeAction(Action):
    def __init__(self, priceables: Union[Priceable, Iterable[Priceable]], trade_duration: str = None, name: str = None):
        """
        create an action which adds a trade when triggered.  The trades are resolved on the trigger date (state) and
        last until the trade_duration if specified or for all future dates if not.
        :param priceables: a priceable or a list of pricables.
        :param trade_duration: an instrument attribute eg. 'expiration_date' or a date or a tenor if left as None the
                               trade will be added for all future dates
        :param name: optional additional name to the priceable name
        """
        # TODO: make trade_duration capable of being a tenor or a date as well as a trade attribute
        super().__init__(name)
        self._priceables = make_list(priceables)
        self._dated_priceables = {}  # a trigger may inject the portfolio at a trigger date
        self._trade_duration = trade_duration
        for i, p in enumerate(self._priceables):
            if p.name is None:
                p.name = '{}_Priceable{}'.format(self._name, i)
            else:
                p.name = '{}_{}'.format(self._name, p.name)

    @property
    def priceables(self):
        return self._priceables

    @property
    def trade_duration(self):
        return self._trade_duration

    def apply_action(self, state: Union[datetime.date, Iterable[datetime.date]], backtest: BackTest):
        with PricingContext(is_batch=True):
            f = {}
            for s in state:
                active_portfolio = self._dated_priceables.get(s) or self._priceables
                with PricingContext(pricing_date=s):
                    f[s] = Portfolio(active_portfolio).resolve(in_place=False)

        for s in backtest.states:
            pos = []
            for create_date, portfolio in f.items():
                pos += [inst for inst in portfolio.result().instruments
                        if get_final_date(inst, create_date, self.trade_duration) >= s >= create_date]
            backtest.portfolio_dict[s].append(pos)

        return backtest


class HedgeAction(Action):
    def __init__(self, risk, priceables: Priceable, trade_duration: str = None, risks_on_final_day: bool = False,
                 name: str = None):
        super().__init__(name)
        self._deterministic = False
        self._priceable = priceables
        self._risk = risk
        self._trade_duration = trade_duration
        self._risks_on_final_day = risks_on_final_day
        if self._priceable.name is None:
            self._priceable.name = '{}_Pricable{}'.format(self._name, 0)
        else:
            self._priceable.name = '{}_{}'.format(self._name, self._priceable.name)

    @property
    def trade_duration(self):
        return self._trade_duration

    def apply_action(self, state: datetime.date, backtest: BackTest):
        with PricingContext(pricing_date=state):
            f = self._priceable.resolve(in_place=False)
            hedge_delta = self._priceable.calc(self.risk)

        hedge = f.result()
        ratio = backtest.results[state][self.risk].aggregate() / hedge_delta.result()
        hedge = scale_trade(hedge, ratio)

        active_dates = [pricing_date for pricing_date in backtest.states if
                        get_final_date(hedge, state, self.trade_duration) >= pricing_date >= state]
        with HistoricalPricingContext(dates=active_dates):
            backtest.calc_calls += 1
            backtest.calculations += len(active_dates) * len(backtest.risks)
            hedge_res = Portfolio(hedge).calc(backtest.risks)

        for pricing_date in active_dates:
            results = hedge_res[pricing_date]
            backtest.add_results(pricing_date, results)
