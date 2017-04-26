import json
from util import generate_uuid
from dataset import Example
from threading import Lock
import src.config as config

class BaseController(object):
    """
    Interface of the controller: takes two systems and can run them to generate a dialgoue.
    """
    def __init__(self, scenario, sessions, chat_id=None, debug=True):

        self.lock = Lock()
        self.scenario = scenario
        self.sessions = sessions
        self.chat_id = chat_id
        assert len(self.sessions) == 2
        if debug:
            for agent in (0, 1):
                self.scenario.kbs[agent].dump()
        self.events = []

    def event_callback(self, event):
        raise NotImplementedError

    def get_outcome(self):
        raise NotImplementedError

    def get_result(self, agent_idx):
        return None

    def simulate(self, max_turns=None):
        '''
        Simulate a dialogue.
        '''
        self.events = []
        time = 0
        num_turns = 0
        game_over = False
        while not game_over:
            for agent, session in enumerate(self.sessions):
                event = session.send()
                time += 1
                if not event:
                    continue

                event.time = time
                self.event_callback(event)
                self.events.append(event)

                print 'agent=%s: session=%s, event=%s' % (agent, type(session).__name__, event.to_dict())
                num_turns += 1
                if self.game_over() or (max_turns and num_turns >= max_turns):
                    game_over = True
                    break

                for partner, other_session in enumerate(self.sessions):
                    if agent != partner:
                        other_session.receive(event)

        uuid = generate_uuid('E')
        outcome = self.get_outcome()
        print 'outcome: %s' % outcome
        # TODO: add configurable names to systems and sessions
        return Example(self.scenario, uuid, self.events, outcome, uuid, None)

    def step(self, backend=None):
        '''
        Called by the web backend.
        '''
        with self.lock:
            # try to send messages from one session to the other(s)
            for agent, session in enumerate(self.sessions):
                if session is None:
                    # fail silently, this means that the session has been reset and the controller is effectively
                    # inactive
                    continue
                event = session.send()
                if event is None:
                    continue

                self.event_callback(event)
                self.events.append(event)
                if backend is not None:
                    backend.add_event_to_db(self.get_chat_id(), event)

                for partner, other_session in enumerate(self.sessions):
                    if agent != partner:
                        other_session.receive(event)

    def inactive(self):
        """
        Return whether this controller is currently controlling an active chat session or not (by checking whether both
        users are still active or not)
        :return: True if the chat is active (if both sessions are not None), False otherwise
        """
        for s in self.sessions:
            if s is None:
                return True
        return False

    def set_inactive(self, agents=[]):
        """
        Set any number of sessions in the Controller to None to mark the Controller as inactive. The default behavior
        is to set all sessions to None (if no parameters are supplied to the function), but a list of indices can be
        passed to set the Session objects at those indices to None.
        :param agents: List of indices of Sessions to mark inactive. If this is None, the function is a no-op. If no
        list is passed, the function sets all Session objects to None.
        """
        with self.lock:
            if agents is None:
                return
            elif len(agents) == 0:
                self.sessions = [None] * len(self.sessions)
            else:
                for idx in agents:
                    self.sessions[idx] = None

    def get_chat_id(self):
        return self.chat_id

    def game_over(self):
        raise NotImplementedError

    def complete(self):
        raise NotImplementedError

class Controller(object):
    '''
    Factory of controllers.
    '''
    @staticmethod
    def get_controller(scenario, sessions, chat_id=None, debug=True):
        if config.task == config.MutualFriends:
            return MutualFriendsController(scenario, sessions, chat_id, debug)
        elif config.task == config.Negotiation:
            return NegotiationController(scenario, sessions, chat_id, debug)
        else:
            raise ValueError('Unknown task: %s.' % config.task)

class MutualFriendsController(BaseController):
    def __init__(self, scenario, sessions, chat_id=None, debug=True):
        super(MutualFriendsController, self).__init__(scenario, sessions, chat_id, debug)
        self.selections = [None, None]

    def event_callback(self, event):
        if event.action == 'select':
            self.selections[event.agent] = event.data

    def get_outcome(self):
        if self.selections[0] is not None and self.selections[0] == self.selections[1]:
            reward = 1
        else:
            reward = 0
        return {'reward': reward}

    def game_over(self):
        return not self.inactive() and self.selections[0] is not None and self.selections[0] == self.selections[1]

    def complete(self):
        return self.selections[0] is not None and self.selections[0] == self.selections[1]

class NegotiationController(BaseController):
    def __init__(self, scenario, sessions, chat_id=None, debug=True):
        super(NegotiationController, self).__init__(scenario, sessions, chat_id, debug)
        self.prices = [None, None]
        self.quit = False

    def event_callback(self, event):
        if event.action == 'offer':
            self.prices[event.agent] = float(event.data)
        elif event.action == 'quit':
            self.quit = True

    def get_outcome(self):
        offer = -1
        if self.prices[0] is not None and self.prices[0] == self.prices[1]:
            reward = 1
            offer = self.prices[0]
        else:
            reward = 0
        return {'reward': reward, 'offer': offer}

    def game_over(self):
        return not self.inactive() and ((self.prices[0] is not None and self.prices[1] is not None) or self.quit)

    def get_result(self, agent_idx):
        if self.prices[0] is None or self.prices[1] is None:
            return None

        result = {agent_idx: {}, 1 - agent_idx: {}}

        result[agent_idx]['Target'] = self.scenario.kbs[agent_idx].facts['personal']['Target']
        result[1 - agent_idx]['Target'] = self.scenario.kbs[1 - agent_idx].facts['personal']['Target']

        result[agent_idx]['Bottomline'] = self.scenario.kbs[agent_idx].facts['personal']['Bottomline']
        result[1 - agent_idx]['Bottomline'] = self.scenario.kbs[1 - agent_idx].facts['personal']['Bottomline']

        result[agent_idx]['Offer'] = int(self.prices[agent_idx])
        result[1 - agent_idx]['Offer'] = int(self.prices[1 - agent_idx])

        return result

    def complete(self):
        return self.prices[0] is not None and self.prices[0] == self.prices[1]

    def get_winner(self):
        if not self.complete():
            return None
        targets = {0: self.scenario.kbs[0].facts['personal']['Target'],
                   1: self.scenario.kbs[1].facts['personal']['Target']}

        roles = {self.scenario.kbs[0].facts['personal']['Role']: 0,
                 self.scenario.kbs[1].facts['personal']['Role']: 1}

        diffs = {}

        # for seller, offer is probably lower than target
        seller_idx = roles['seller']
        diffs[seller_idx] = targets[seller_idx] - self.prices[seller_idx]
        if diffs[seller_idx] < 0:
            diffs[seller_idx] = -diffs[seller_idx]

        # for buyer, offer is probably
        buyer_idx = roles['buyer']
        diffs[buyer_idx] = self.prices[buyer_idx] - targets[buyer_idx]
        if diffs[buyer_idx] < 0:
            diffs[buyer_idx] = -diffs[buyer_idx]

        if diffs[0] < diffs[1]:
            return 0
        elif diffs[1] < diffs[0]:
            return 1

        return -1