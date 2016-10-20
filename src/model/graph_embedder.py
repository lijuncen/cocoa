import tensorflow as tf
from tensorflow.python.ops.math_ops import tanh
from tensorflow.python.ops.rnn_cell import _linear as linear
from util import batch_embedding_lookup, batch_linear

def add_graph_embed_arguments(parser):
    parser.add_argument('--node-embed-size', default=50, help='Knowledge graph node/subgraph embedding size')
    parser.add_argument('--edge-embed-size', default=20, help='Knowledge graph edge label embedding size')
    parser.add_argument('--entity-embed-size', default=50, help='Knowledge graph entity embedding size')
    parser.add_argument('--entity-cache-size', type=int, default=2, help='Number of entities to remember (this is more of a performance concern; ideally we can remember all entities within the history)')
    parser.add_argument('--use-entity-embedding', action='store_true', default=False, help='Whether to use entity embedding when compute node embeddings')
    parser.add_argument('--mp-iters', type=int, default=1, help='Number of iterations of message passing on the graph')
    parser.add_argument('--combine-message', default='concat', help='How to combine propogated message {concat, sum}')

class GraphEmbedderConfig(object):
    def __init__(self, node_embed_size, edge_embed_size, graph_metadata, entity_embed_size=None, use_entity_embedding=False, mp_iters=2, message_combiner='concat', batch_size=1):
        self.node_embed_size = node_embed_size

        self.num_edge_labels = graph_metadata.relation_map.size
        self.edge_embed_size = edge_embed_size

        # RNN output size
        self.utterance_size = graph_metadata.utterance_size
        # Maximum number of nodes/entities to update embeddings for
        self.entity_cache_size = graph_metadata.entity_cache_size

        # Size of input features from Graph
        self.feat_size = graph_metadata.feat_size

        # Number of message passing iterations
        self.mp_iters = mp_iters
        # How to combine messages from each iteration
        self.message_combiner = message_combiner
        if message_combiner == 'concat':
            self.context_size = self.node_embed_size * (mp_iters + 1)
        elif message_combiner == 'sum':
            self.context_size = self.node_embed_size
        else:
            raise ValueError('Unknown message combiner')

        self.use_entity_embedding = use_entity_embedding
        if use_entity_embedding:
            self.num_entities = graph_metadata.entity_map.size
            self.entity_embed_size = entity_embed_size

        self.batch_size = batch_size

        # padding
        self.pad_path_id = graph_metadata.PAD_PATH_ID
        self.node_pad = graph_metadata.NODE_PAD

class GraphEmbedder(object):
    '''
    Graph embedding model.
    '''
    def __init__(self, config, scope=None):
        self.config = config
        self.scope = scope
        self.context_initialized = False
        self.build_model(scope)

    def build_model(self, scope=None):
        with tf.variable_scope(scope or type(self).__name__):
            with tf.variable_scope('EdgeEmbedding'):
                self.edge_embedding = tf.get_variable('edge', [self.config.num_edge_labels, self.config.edge_embed_size])

            if self.config.use_entity_embedding:
                with tf.variable_scope('EntityEmbedding'):
                    self.entity_embedding = tf.get_variable('entity', [self.config.num_entities, self.config.entity_embed_size])

            # TODO: make batch_size a variable so that we can feed in batches with arbitray batch size
            with tf.name_scope('Inputs'):
                batch_size = self.config.batch_size
                # Nodes in the Graph, id is row index in utterances.
                # The number of nodes can vary in each batch.
                node_ids = tf.placeholder(tf.int32, shape=[batch_size, None], name='node_ids')
                mask = tf.placeholder(tf.bool, shape=[batch_size, None], name='mask')

                # Entity ids used for look up in entity_embedding when use_entity_embedding.
                # NOTE: node_ids is local; it's essentially range(number of nodes). entity_ids
                # use the global entity mapping.
                entity_ids = tf.placeholder(tf.int32, shape=[batch_size, None], name='entity_ids')

                # A path is a tuple of (node_id, edge_label, node_id)
                # NOTE: we assume the first path is always a padding path (NODE_PAD, EDGE_PAD,
                # NODE_PAD) when computing mask in pass_message
                # The number of paths can vary in each batch.
                paths = tf.placeholder(tf.int32, shape=[batch_size, None, 3], name='paths')

                # Each node has a list of paths starting from that node. path id is row index
                # in paths. Paths of padded nodes are PATH_PAD.
                node_paths = tf.placeholder(tf.int32, shape=[batch_size, None, None], name='node_paths')

                # Node features. NOTE: feats[i] must corresponds to node_ids[i]
                node_feats = tf.placeholder(tf.float32, shape=[batch_size, None, self.config.feat_size], name='node_feats')

                self.input_data = (node_ids, mask, entity_ids, paths, node_paths, node_feats)

            # This will be used by GraphDecoder to figure out the shape of the output attention scores
            self.node_ids = self.input_data[0]

    def get_context(self, utterances):
        '''
        Compute embedding of each node as context for the attention model.
        utterances: current utterance embeddings from the dialogue history
        '''
        node_ids, mask, entity_ids, paths, node_paths, node_feats = self.input_data
        with tf.variable_scope(self.scope or type(self).__name__):
            with tf.variable_scope('NodeEmbedding'):
                with tf.variable_scope('InitNodeEmbedding') as scope:
                    # It saves some reshapes to do batch_linear and batch_embedding_lookup
                    # together, but this way is clearer.
                    # TODO: initial_node_embed: just concat is fine, don't do linear
                    if self.config.use_entity_embedding:
                        initial_node_embed = batch_linear([tf.nn.embedding_lookup(self.entity_embedding, entity_ids), batch_embedding_lookup(utterances, node_ids), node_feats], self.config.node_embed_size, True)
                    else:
                        initial_node_embed = batch_linear([batch_embedding_lookup(utterances, node_ids), node_feats], self.config.node_embed_size, True)
                    scope.reuse_variables()

                # Message passing
                def mp(curr_node_embedding):
                    messages = self.embed_path(curr_node_embedding, self.edge_embedding, paths)
                    return self.pass_message(messages, node_paths, self.config.pad_path_id)
                node_embeds = tf.scan(lambda curr_embed, _: mp(curr_embed), \
                        tf.range(0, self.config.mp_iters), \
                        initial_node_embed)  # (mp_iters, batch_size, num_nodes, embed_size)

        if self.config.message_combiner == 'concat':
            context = tf.concat(2, [initial_node_embed] + tf.unpack(node_embeds, axis=0))
        elif self.config.message_combiner == 'sum':
            context = tf.add_n([initial_node_embed] + tf.unpack(node_embeds, axis=0))
        else:
            raise ValueError('Unknown message combining method')

        # Set padded context to zero
        context = context * tf.to_float(tf.expand_dims(mask, 2))

        self.context_initialized = True
        return context, mask

    def embed_path(self, node_embedding, edge_embedding, paths):
        '''
        Compute embedding of a path (edge_label, node_id).
        node_embedding: (batch_size, num_nodes, node_embed_size)
        edge_embedding: (num_edge_label, edge_embed_size)
        paths: each path is a tuple of (node_id, edge_label, node_id).
        (batch_size, num_paths, 3)
        '''
        edge_embeds = tf.nn.embedding_lookup(edge_embedding, paths[:, :, 1])
        node_embeds = batch_embedding_lookup(node_embedding, paths[:, :, 2])
        path_embed_size = self.config.node_embed_size
        path_embeds = tanh(batch_linear([edge_embeds, node_embeds], path_embed_size, True))
        return path_embeds

    def pass_message(self, path_embeds, neighbors, padded_path=0):
        '''
        Compute new node embeddings by summing path embeddings (message) of neighboring nodes.
        neighbors: ids of neighboring paths of each node where id is row index in path_embeds
        (batch_size, num_nodes, num_neighbors)
        path_embeds: (batch_size, num_paths, path_embed_size)
        PATH_PAD: if a node is not incident to any edge, its path ids in neighbors are PATH_PAD
        '''
        # Mask padded nodes in neighbors
        # NOTE: although we mask padded nodes in get_context, we still need to mask neighbors
        # for entities not in the KB but mentioned by the partner. These are dangling nodes
        # and should not have messages passed in.
        mask = tf.to_float(tf.not_equal(neighbors, tf.constant(padded_path)))  # (batch_size, num_nodes, num_neighbors)

        # Use static shape when possible
        shape = tf.shape(neighbors)
        batch_size, num_nodes, _ = neighbors.get_shape().as_list()
        batch_size = batch_size or shape[0]
        num_nodes = num_nodes or shape[1]
        path_embed_size = path_embeds.get_shape().as_list()[-1]

        # Gather neighboring path embeddings
        neighbors = tf.reshape(neighbors, [batch_size, -1])  # (batch_size, num_nodes x num_neighbors)
        embeds = batch_embedding_lookup(path_embeds, neighbors)  # (batch_size, num_nodes x num_neighbors, path_embed_size)
        embeds = tf.reshape(embeds, [batch_size, num_nodes, -1, path_embed_size])
        mask = tf.expand_dims(mask, 3)  # (batch_size, num_nodes, num_neighbors, 1)
        embeds = embeds * mask
        new_node_embeds = tf.reduce_sum(embeds, 2)  # (batch_size, num_nodes, path_embed_size)

        # The for-loop version
        #new_node_embeds = []
        #num_neighbors = neighbors.get_shape().as_list()[-1]
        #path_embed_size = path_embeds.get_shape().as_list()[-1]
        #for i in xrange(self.config.batch_size):
        #    node_inds = tf.reshape(neighbors[i], [-1])  # (num_nodes x num_neighbors)
        #    batch_mask = tf.reshape(mask[i], [-1, 1])  # (num_nodes x num_neighbors, 1)
        #    embeds = tf.gather(path_embeds[i], node_inds) * batch_mask  # (num_nodes x num_neighbors, path_embed_size)
        #    embeds = tf.reduce_sum(tf.reshape(embeds, [-1, num_neighbors, path_embed_size]), 1)  # (num_nodes, path_embed_size)
        #    new_node_embeds.append(embeds)
        #new_node_embeds = tf.pack(new_node_embeds)

        return new_node_embeds

    def update_utterance(self, entity_indices, utterance, curr_utterances):
        '''
        We first transform utterance into a dense matrix of the same size as curr_utterances,
        then return their sum.
        entity_indices: entity ids correponding to rows to be updated in the curr_utterances
        (batch_size, entity_cache_size)
        utterance: hidden states from the RNN
        (batch_size, utterance_size)
        NOTE: each curr_utterance matrix should have a row (e.g. the last one) as padded utterance.
        Padded entities in entity_indices corresponds to the padded utterance. This is handled
        by GraphBatch during construnction of the input data.
        '''
        B = self.config.batch_size
        E = self.config.entity_cache_size
        U = self.config.utterance_size
        # Construct indices corresponding to each entry to be updated in self.utterances
        # self.utterance has shape (batch_size, num_nodes, utterance_size)
        # Therefore each row in the indices matrix specifies (batch_id, node_id, utterance_dim)
        batch_inds = tf.reshape(tf.tile(tf.reshape(tf.range(B), [-1, 1]), [1, E*U]), [-1, 1])
        node_inds = tf.reshape(tf.tile(tf.reshape(entity_indices, [-1, 1]), [1, U]), [-1, 1])
        utterance_inds = tf.reshape(tf.tile(tf.range(U), [E*B]), [-1, 1])
        inds = tf.concat(1, [batch_inds, node_inds, utterance_inds])

        # Repeat utterance for each entity
        utterance = tf.reshape(tf.tile(utterance, [1, E]), [-1])
        new_utterance = tf.sparse_to_dense(inds, tf.shape(curr_utterances), utterance, validate_indices=False)
        return curr_utterances + new_utterance
