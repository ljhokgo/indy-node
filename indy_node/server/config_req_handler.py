from typing import List

from indy_common.authorize.auth_constraints import ConstraintCreator
from indy_common.authorize.auth_actions import AuthActionEdit, AuthActionAdd, EDIT_PREFIX, ADD_PREFIX
from indy_common.config_util import getConfig
from plenum.common.exceptions import InvalidClientRequest, InvalidMessageException
from plenum.common.txn_util import reqToTxn, is_forced, get_payload_data, append_txn_metadata
from plenum.server.ledger_req_handler import LedgerRequestHandler
from plenum.common.constants import TXN_TYPE, NAME, VERSION, FORCE
from indy_common.constants import POOL_UPGRADE, START, CANCEL, SCHEDULE, ACTION, POOL_CONFIG, NODE_UPGRADE, PACKAGE, \
    APP_NAME, REINSTALL, AUTH_RULE, CONSTRAINT, AUTH_ACTION, OLD_VALUE, NEW_VALUE, AUTH_TYPE, FIELD
from indy_common.types import Request, ClientAuthRuleOperation
from indy_node.persistence.idr_cache import IdrCache
from indy_node.server.upgrader import Upgrader
from indy_node.server.pool_config import PoolConfig
from indy_node.utils.node_control_utils import NodeControlUtil


class ConfigReqHandler(LedgerRequestHandler):
    write_types = {POOL_UPGRADE, NODE_UPGRADE, POOL_CONFIG, AUTH_RULE}

    def __init__(self, ledger, state, idrCache: IdrCache,
                 upgrader: Upgrader, poolManager, poolCfg: PoolConfig,
                 write_req_validator):
        super().__init__(ledger, state)
        self.idrCache = idrCache
        self.upgrader = upgrader
        self.poolManager = poolManager
        self.poolCfg = poolCfg
        self.write_req_validator = write_req_validator

    def doStaticValidation(self, request: Request):
        identifier, req_id, operation = request.identifier, request.reqId, request.operation
        if operation[TXN_TYPE] == POOL_UPGRADE:
            self._doStaticValidationPoolUpgrade(identifier, req_id, operation)
        elif operation[TXN_TYPE] == POOL_CONFIG:
            self._doStaticValidationPoolConfig(identifier, req_id, operation)
        elif operation[TXN_TYPE] == AUTH_RULE:
            self._doStaticValidationAuthRule(identifier, req_id, operation)

    def _doStaticValidationPoolConfig(self, identifier, reqId, operation):
        pass

    def _doStaticValidationAuthRule(self, identifier, reqId, operation):
        constraint = operation.get(CONSTRAINT)
        ConstraintCreator.create_constraint(constraint)
        action = operation.get(AUTH_ACTION, None)
        try:
            auth_key = self.get_auth_key(operation)
        except Exception:
            transaction_schema = dict(ClientAuthRuleOperation.schema)
            if action == ADD_PREFIX:
                transaction_schema.pop(OLD_VALUE)
            raise InvalidClientRequest(identifier, reqId,
                                       "Transaction for {} authentication "
                                       "rules must match the schema = {}".
                                       format(action,
                                              transaction_schema.keys()))

        if auth_key not in self.write_req_validator.auth_map and \
                auth_key not in self.write_req_validator.anyone_can_write_map:
            raise InvalidClientRequest(identifier, reqId,
                                       "Key '{}' is not contained in the "
                                       "authorization map".format(auth_key))

    def _doStaticValidationPoolUpgrade(self, identifier, reqId, operation):
        action = operation.get(ACTION)
        if action not in (START, CANCEL):
            raise InvalidClientRequest(identifier, reqId,
                                       "{} not a valid action".
                                       format(action))
        if action == START:
            schedule = operation.get(SCHEDULE, {})
            force = operation.get(FORCE)
            force = str(force) == 'True'
            isValid, msg = self.upgrader.isScheduleValid(
                schedule, self.poolManager.getNodesServices(), force)
            if not isValid:
                raise InvalidClientRequest(identifier, reqId,
                                           "{} not a valid schedule since {}".
                                           format(schedule, msg))

        # TODO: Check if cancel is submitted before start

    def curr_pkt_info(self, pkg_name):
        if pkg_name == APP_NAME:
            return Upgrader.getVersion(), [APP_NAME]
        return NodeControlUtil.curr_pkt_info(pkg_name)

    def validate(self, req: Request):
        status = '*'
        operation = req.operation
        typ = operation.get(TXN_TYPE)
        if typ not in [POOL_UPGRADE, POOL_CONFIG, AUTH_RULE]:
            return
        if typ == POOL_UPGRADE:
            pkt_to_upgrade = req.operation.get(PACKAGE, getConfig().UPGRADE_ENTRY)
            if pkt_to_upgrade:
                currentVersion, cur_deps = self.curr_pkt_info(pkt_to_upgrade)
                if not currentVersion:
                    raise InvalidClientRequest(req.identifier, req.reqId,
                                               "Packet {} is not installed and cannot be upgraded".
                                               format(pkt_to_upgrade))
                if all([APP_NAME not in d for d in cur_deps]):
                    raise InvalidClientRequest(req.identifier, req.reqId,
                                               "Packet {} doesn't belong to pool".format(pkt_to_upgrade))
            else:
                raise InvalidClientRequest(req.identifier, req.reqId, "Upgrade packet name is empty")

            targetVersion = req.operation[VERSION]
            reinstall = req.operation.get(REINSTALL, False)
            if not Upgrader.is_version_upgradable(currentVersion, targetVersion, reinstall):
                # currentVersion > targetVersion
                raise InvalidClientRequest(req.identifier, req.reqId, "Version is not upgradable")

            action = operation.get(ACTION)
            # TODO: Some validation needed for making sure name and version
            # present
            txn = self.upgrader.get_upgrade_txn(
                lambda txn: get_payload_data(txn).get(
                    NAME,
                    None) == req.operation.get(
                    NAME,
                    None) and get_payload_data(txn).get(VERSION) == req.operation.get(VERSION),
                reverse=True)
            if txn:
                status = get_payload_data(txn).get(ACTION, '*')

            if status == START and action == START:
                raise InvalidClientRequest(
                    req.identifier,
                    req.reqId,
                    "Upgrade '{}' is already scheduled".format(
                        req.operation.get(NAME)))
            if status == '*':
                auth_action = AuthActionAdd(txn_type=POOL_UPGRADE,
                                            field=ACTION,
                                            value=action)
            else:
                auth_action = AuthActionEdit(txn_type=POOL_UPGRADE,
                                             field=ACTION,
                                             old_value=status,
                                             new_value=action)
            self.write_req_validator.validate(req,
                                              [auth_action])
        elif typ == POOL_CONFIG:
            action = '*'
            status = '*'
            self.write_req_validator.validate(req,
                                              [AuthActionEdit(txn_type=typ,
                                                              field=ACTION,
                                                              old_value=status,
                                                              new_value=action)])
        elif typ == AUTH_RULE:
            self.write_req_validator.validate(req,
                                              [AuthActionEdit(txn_type=typ,
                                                              field="*",
                                                              old_value="*",
                                                              new_value="*")])

    def apply(self, req: Request, cons_time):
        txn = append_txn_metadata(reqToTxn(req),
                                  txn_time=cons_time)
        self.ledger.append_txns_metadata([txn])
        (start, _), _ = self.ledger.appendTxns([txn])
        return start, txn

    def commit(self, txnCount, stateRoot, txnRoot, ppTime) -> List:
        committedTxns = super().commit(txnCount, stateRoot, txnRoot, ppTime)
        for txn in committedTxns:
            # Handle POOL_UPGRADE or POOL_CONFIG transaction here
            # only in case it is not forced.
            # If it is forced then it was handled earlier
            # in applyForced method.
            if not is_forced(txn):
                self.upgrader.handleUpgradeTxn(txn)
                self.poolCfg.handleConfigTxn(txn)
        return committedTxns

    def applyForced(self, req: Request):
        super().applyForced(req)
        txn = reqToTxn(req)
        self.upgrader.handleUpgradeTxn(txn)
        self.poolCfg.handleConfigTxn(txn)

    @staticmethod
    def get_auth_key(operation):
        action = operation.get(AUTH_ACTION, None)
        if OLD_VALUE not in operation and action == EDIT_PREFIX:
            raise InvalidMessageException("Request for edit auth rule must contains "
                                          "a field '{}'.".format(OLD_VALUE))

        old_value = operation.get(OLD_VALUE, None)
        new_value = operation.get(NEW_VALUE, None)
        auth_type = operation.get(AUTH_TYPE, None)
        field = operation.get(FIELD, None)

        return AuthActionEdit(txn_type=auth_type,
                              field=field,
                              old_value=old_value,
                              new_value=new_value).get_action_id() \
            if action == EDIT_PREFIX else \
            AuthActionAdd(txn_type=auth_type,
                          field=field,
                          value=new_value).get_action_id()
