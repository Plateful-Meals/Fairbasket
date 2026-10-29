#!/usr/bin/env python3
"""
Fairbasket: starter matching rules for the products that have no hand-written rules in config.json.

config.json holds careful, hand-tested rules for the most-used ingredients. The rest of the site's products had none,
so they were never priced. This script writes pipeline/auto-rules.json: one simpler rule per remaining product,
made from its name on the site (the words that must appear in a shop's product name) plus a short list of
hand-made fixes where shops use different words ("easy peelers" are sold as "clementines" or "satsumas").

Auto rules are deliberately cautious:
  - every main word of the name must appear in the shop's product name (any plural);
  - the global "avoid" groups in config.json still apply (organic, premium, flavoured, ready meals ...), except a
    group that the product's own name belongs to (a "smoked haddock" may say smoked, "chocolate chip cookies" are
    biscuits);
  - things that are a different product altogether (sauce, soup, pie, kit, meal, crisps ...) are rejected unless
    the product's own name contains that word;
  - every price still has to pass the pack-size check, the price sanity check and the safety guard.
They are marked "auto": true. Only the shop-price sources that ask for them use them (the Pepesto robot does;
the Aldi reader, the Google search and the home checker keep using config.json alone unless told otherwise).
A product moved into config.json with hand-written rules is left out of this file automatically.

  python pipeline/auto_rules.py            write pipeline/auto-rules.json (optional: without it, the Pepesto robot
                                           makes the same rules itself on every run, so new products are picked up)
  python pipeline/auto_rules.py --show     print the rules instead
"""
import argparse, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# words in a product's name on the site that do not have to appear in the shop's product name
SOFT = {'fresh', 'frozen', 'pack', 'packs', 'plain', 'ready', 'rolled', 'tin', 'tinned', 'in', 'of', 'and', 'with',
        'the', 'a', 'style', 'pieces', 'slices', 'sliced', 'joint', 'meal', 'drink', 'bar', 'bars', 'log', 'cereal',
        'loaf', 'jar', 'bottle', 'bag', 'whole', 'ground', 'dried', 'cooked', 'raw', 'large', 'fillets', 'fillet',
        'steaks', 'steak', 'spread', 'sauce', 'paste', 'free', 'range', 'oil', 'bites', 'water', 'juice', 'apple'}
# (a name made only of these words keeps them, e.g. "Still water")

# a different product altogether; rejected unless the product's own name contains the word
OTHER = ['sauce', 'soup', 'pie', 'kit', 'meal', 'crisps', 'sandwich', 'salad', 'pizza', 'dip', 'spread', 'paste',
         'seasoning', 'mix', 'stock', 'gravy', 'cake', 'biscuit', 'bar', 'drink', 'juice', 'yoghurt', 'cream', 'cheese',
         'chocolate', 'jam', 'pot', 'snack', 'bites', 'nuggets', 'burger', 'oil', 'vinegar', 'powder', 'flavour',
         'flavoured', 'shampoo', 'conditioner', 'refill', 'wipes', 'pudding', 'custard', 'coated', 'breaded',
         'battered', 'marinated', 'kiev', 'ready meal', 'microwave', 'tablets', 'capsules', 'pods', 'gift', 'selection']

# hand-made fixes: product id -> fields that replace the generated ones
FIX = {
    'easy_peelers': {'must': [r'clementines?|satsumas?|easy ?peelers?|mandarins?|tangerines?|easy peeler']},
    'kiwi': {'must': [r'kiwi']},
    'limes': {'must': [r'\blimes?\b']},
    'mango': {'must': [r'\bmangoe?s?\b'], 'extra_avoid': r'chutney|dried|frozen|chunks|juice|smoothie|lassi'},
    'pineapple': {'must': [r'pineapple'], 'extra_avoid': r'chunks|rings|tin|juice|dried|slices'},
    'red_grapes': {'must': [r'\bred\b', r'grapes?']},
    'green_grapes': {'must': [r'green|white', r'grapes?']},
    'fresh_mint': {'must': [r'\bmint\b'], 'allow': ['flavour'], 'extra_avoid': r'sauce|jelly|tea|choc|imperials|humbugs|ice'},
    'fresh_thyme': {'must': [r'thyme'], 'allow': ['flavour'], 'extra_avoid': r'dried|jar'},
    'fresh_rosemary': {'must': [r'rosemary'], 'allow': ['flavour'], 'extra_avoid': r'dried|jar'},
    'fresh_chives': {'must': [r'chives?'], 'allow': ['flavour'], 'extra_avoid': r'dried|jar|cheese|dip'},
    'beef_sirloin': {'must': [r'sirloin']},
    'beef_rump': {'must': [r'\brump\b']},
    'beef_ribeye': {'must': [r'rib ?eye']},
    'beef_roasting': {'must': [r'beef', r'joint|topside|silverside|roasting']},
    'lamb_leg_steaks': {'must': [r'lamb', r'leg', r'steaks?']},
    'lamb_chops': {'must': [r'lamb', r'chops?|cutlets?']},
    'lamb_diced': {'must': [r'lamb', r'diced']},
    'pork_belly': {'must': [r'pork', r'belly']},
    'pork_shoulder': {'must': [r'pork', r'shoulder']},
    'pork_chops': {'must': [r'pork', r'chops?']},
    'pork_ribs': {'must': [r'pork', r'ribs?'], 'allow': ['flavour'], 'extra_avoid': r'bbq|barbecue|sticky|chinese|glazed|marinated|honey|sauce'},
    'streaky_bacon': {'must': [r'streaky'], 'allow': ['flavour']},
    'chicken_legs': {'must': [r'chicken', r'legs?']},
    'chicken_mini_fillets': {'must': [r'chicken', r'mini fillets?|mini breast'], 'allow': ['kids']},
    'chicken_diced': {'must': [r'chicken', r'diced']},
    'chipolatas': {'must': [r'chipolatas?'], 'extra_avoid': r'bacon|wrapped|pigs in'},
    'cumberland_sausages': {'must': [r'cumberland'], 'extra_avoid': r'ring|swirl'},
    'salami': {'must': [r'salami'], 'extra_avoid': r'pizza|sticks?'},
    'hot_dogs': {'must': [r'hot ?dogs?|frankfurters?'], 'extra_avoid': r'rolls?|buns?'},
    'smoked_haddock': {'must': [r'smoked', r'haddock']},
    'smoked_mackerel': {'must': [r'smoked', r'mackerel']},
    'raw_king_prawns': {'must': [r'king prawns?'], 'extra_avoid': r'cooked'},
    'cooked_prawns': {'must': [r'prawns?'], 'extra_avoid': r'king|tiger|raw|cocktail|ring'},
    'smoked_salmon': {'must': [r'smoked', r'salmon'], 'extra_avoid': r'trimmings|pate|pâté|fillets?|hot ?smoked|flakes'},
    'fish_pie_mix': {'must': [r'fish pie'], 'extra_avoid': r'ready|meal'},
    'tinned_salmon': {'must': [r'pink salmon|red salmon|salmon'], 'extra_avoid': r'fillets?|smoked|fresh'},
    'tinned_sardines': {'must': [r'sardines?', r'tomato']},
    'tinned_mackerel': {'must': [r'mackerel'], 'extra_avoid': r'smoked|fresh|tomato|peppered'},
    'tuna_sunflower': {'must': [r'tuna', r'sunflower|oil']},
    'mozzarella_ball': {'must': [r'mozzarella'], 'extra_avoid': r'grated|shredded|sticks|pizza|light|mini|pearls'},
    'cream_cheese': {'must': [r'soft cheese|cream cheese'], 'extra_avoid': r'light|lighter|garlic|herb|chive|reduced'},
    'goats_cheese': {'must': [r'goats?\'? ?cheese|goat\'?s cheese']},
    'red_leicester': {'must': [r'red leicester']},
    'mild_cheddar': {'must': [r'mild', r'cheddar'], 'extra_avoid': r'grated|slices|sliced'},
    'extra_mature_cheddar': {'must': [r'extra mature', r'cheddar'], 'extra_avoid': r'grated|slices|sliced|vintage'},
    'cheese_spread': {'must': [r'cheese', r'triangles|spread'], 'allow': ['prepared']},
    'unsalted_butter': {'must': [r'unsalted', r'butter']},
    'spreadable_butter': {'must': [r'spreadable|lighter spreadable']},
    'creme_fraiche': {'must': [r'cr[eè]me fra[iî]che']},
    'evaporated_milk': {'must': [r'evaporated']},
    'condensed_milk': {'must': [r'condensed']},
    'fruit_yoghurt': {'must': [r'yogh?urts?'], 'allow': ['flavour', 'kids'], 'extra_avoid': r'greek|natural|plain|drink'},
    'skyr': {'must': [r'skyr']},
    'chilled_custard': {'must': [r'custard'], 'extra_avoid': r'powder|tin|creams|tarts?|slices?|doughnuts?'},
    'rice_pudding': {'must': [r'rice pudding']},
    'large_eggs': {'must': [r'\beggs?\b', r'large']},
    'english_muffins': {'must': [r'muffins?'], 'extra_avoid': r'chocolate|blueberry|double|lemon|cake'},
    'brioche_rolls': {'must': [r'brioche']},
    'sourdough': {'must': [r'sourdough']},
    'seeded_loaf': {'must': [r'seeded|seed', r'bread|loaf|batch']},
    'garlic_bread': {'must': [r'garlic', r'bread|baguette'], 'allow': ['flavour']},
    'croissants': {'must': [r'croissants?']},
    'pain_au_chocolat': {'must': [r'pain au chocolat|pains au chocolat'], 'allow': ['prepared']},
    'teacakes': {'must': [r'teacakes?']},
    'wholemeal_wraps': {'must': [r'wholemeal|whole ?wheat', r'wraps?|tortillas?'], 'allow': ['prepared']},
    'flatbreads': {'must': [r'flatbreads?']},
    'ciabatta': {'must': [r'ciabatta'], 'extra_avoid': r'garlic|rolls?|pizza'},
    'hot_dog_rolls': {'must': [r'hot ?dog', r'rolls?|buns?']},
    'bread_flour': {'must': [r'bread flour|strong']},
    'wholemeal_flour': {'must': [r'wholemeal', r'flour']},
    'pilau_rice': {'must': [r'pilau']},
    'coconut_cream': {'must': [r'coconut cream|creamed coconut']},
    'tinned_peaches': {'must': [r'peach']},
    'tinned_peas': {'must': [r'peas'], 'extra_avoid': r'frozen|mushy|chick|sugar|split'},
    'black_olives': {'must': [r'black', r'olives?']},
    'green_olives': {'must': [r'green', r'olives?']},
    'worcester_sauce': {'must': [r'worcester']},
    'brown_sauce': {'must': [r'brown sauce|\bhp\b']},
    'mint_sauce': {'must': [r'mint sauce'], 'allow': ['flavour']},
    'horseradish': {'must': [r'horseradish']},
    'hot_sauce': {'must': [r'hot sauce|hot pepper sauce|tabasco|frank'], 'allow': ['flavour']},
    'sriracha': {'must': [r'sriracha'], 'allow': ['flavour']},
    'mango_chutney': {'must': [r'mango chutney']},
    'english_mustard': {'must': [r'english mustard']},
    'wholegrain_mustard': {'must': [r'wholegrain mustard']},
    'sesame_oil': {'must': [r'sesame oil']},
    'coconut_oil': {'must': [r'coconut oil']},
    'sunflower_oil': {'must': [r'sunflower oil']},
    'rapeseed_oil': {'must': [r'rapeseed oil']},
    'extra_virgin_olive_oil': {'must': [r'extra virgin']},
    'oyster_sauce': {'must': [r'oyster sauce']},
    'hoisin_sauce': {'must': [r'hoisin']},
    'fish_sauce': {'must': [r'fish sauce']},
    'teriyaki_sauce': {'must': [r'teriyaki'], 'allow': ['flavour']},
    'korma_sauce': {'must': [r'korma'], 'allow': ['flavour']},
    'madras_sauce': {'must': [r'madras'], 'allow': ['flavour']},
    'bolognese_sauce': {'must': [r'bolognese']},
    'carbonara_sauce': {'must': [r'carbonara']},
    'arrabbiata_sauce': {'must': [r'arrabbiata'], 'allow': ['flavour']},
    'four_cheese_sauce': {'must': [r'cheese', r'sauce']},
    'red_pesto': {'must': [r'red pesto|pesto rosso|sun[- ]?dried tomato pesto']},
    'taco_kit': {'must': [r'taco'], 'extra_avoid': r'seasoning|shells? only|sauce'},
    'thai_green_paste': {'must': [r'thai green'], 'allow': ['flavour']},
    'thai_red_paste': {'must': [r'thai red'], 'allow': ['flavour']},
    'beef_stock_pot': {'must': [r'beef', r'stock pots?|stockpots?']},
    'chicken_stock_pot': {'must': [r'chicken', r'stock pots?|stockpots?']},
    'smoked_paprika': {'must': [r'smoked paprika'], 'allow': ['flavour']},
    'five_spice': {'must': [r'five spice|5 spice']},
    'ras_el_hanout': {'must': [r'ras el hanout']},
    'jerk_seasoning': {'must': [r'jerk'], 'allow': ['flavour']},
    'cajun_seasoning': {'must': [r'cajun'], 'allow': ['flavour']},
    'mixed_herbs': {'must': [r'mixed herbs'], 'allow': ['flavour']},
    'dried_thyme': {'must': [r'thyme'], 'allow': ['flavour'], 'extra_avoid': r'fresh'},
    'dried_rosemary': {'must': [r'rosemary'], 'allow': ['flavour'], 'extra_avoid': r'fresh'},
    'dried_parsley': {'must': [r'parsley'], 'allow': ['flavour'], 'extra_avoid': r'fresh'},
    'sea_salt_flakes': {'must': [r'sea salt']},
    'golden_syrup': {'must': [r'golden syrup']},
    'maple_syrup': {'must': [r'maple'], 'allow': ['flavour']},
    'soft_brown_sugar': {'must': [r'brown', r'sugar']},
    'bicarbonate': {'must': [r'bicarbonate']},
    'dried_yeast': {'must': [r'yeast'], 'extra_avoid': r'extract|marmite|spread'},
    'vanilla_extract': {'must': [r'vanilla', r'extract|essence']},
    'dark_chocolate_baking': {'must': [r'dark', r'chocolate'], 'extra_avoid': r'chips|drops|buttons|mint|orange'},
    'chocolate_chips': {'must': [r'chocolate', r'chips|drops|chunks']},
    'ground_almonds': {'must': [r'ground almonds']},
    'custard_powder': {'must': [r'custard'], 'extra_avoid': r'pot|ready|carton|tin|tart|cream'},
    'dates': {'must': [r'\bdates\b']},
    'peanuts': {'must': [r'peanuts'], 'extra_avoid': r'butter|chilli|honey|dry roasted'},
    'cashews': {'must': [r'cashew']},
    'flaked_almonds': {'must': [r'flaked almonds']},
    'walnuts': {'must': [r'walnut']},
    'pine_nuts': {'must': [r'pine nuts']},
    'mixed_nuts': {'must': [r'mixed nuts|nut mix']},
    'chia_seeds': {'must': [r'chia']},
    'cornflakes': {'must': [r'corn ?flakes']},
    'bran_flakes': {'must': [r'bran flakes']},
    'wheat_biscuit_cereal': {'must': [r'wheat ?biscuits?|weetabix|wheat bisks'], 'allow': ['prepared']},
    'muesli': {'must': [r'muesli']},
    'granola': {'must': [r'granola'], 'allow': ['flavour'], 'extra_avoid': r'bar|bites'},
    'chocolate_cereal': {'must': [r'choc', r'rice|crispies|pops|snaps']},
    'rice_cereal': {'must': [r'rice', r'crispies|pops|snaps|krispies'], 'extra_avoid': r'choc'},
    'instant_porridge': {'must': [r'porridge|oat', r'sachets?|pots?|instant|so easy|oat so'], 'allow': ['flavour']},
    'hazelnut_spread': {'must': [r'hazelnut|nutella']},
    'yeast_extract': {'must': [r'yeast extract|marmite']},
    'orange_marmalade': {'must': [r'marmalade']},
    'crunchy_peanut_butter': {'must': [r'peanut butter', r'crunchy']},
    'pancake_mix': {'must': [r'pancake', r'mix|batter']},
    'tea_bags': {'must': [r'\btea\b'], 'extra_avoid': r'green|herbal|fruit|peppermint|chamomile|decaf|iced|camomile|earl grey|redbush'},
    'green_tea': {'must': [r'green tea']},
    'instant_coffee': {'must': [r'coffee'], 'extra_avoid': r'ground|beans|pods|capsules|bags|latte|cappuccino|mocha|decaf'},
    'ground_coffee': {'must': [r'ground', r'coffee']},
    'hot_chocolate': {'must': [r'hot chocolate|drinking chocolate']},
    'cranberry_juice': {'must': [r'cranberry']},
    'orange_squash': {'must': [r'orange', r'squash|cordial|dilut']},
    'blackcurrant_squash': {'must': [r'blackcurrant|ribena', r'squash|cordial|dilut']},
    'cola': {'must': [r'\bcola\b|coca-cola|pepsi'], 'extra_avoid': r'diet|zero|max|cherry|vanilla|sugar free|lime'},
    'lemonade': {'must': [r'lemonade'], 'allow': ['flavour'], 'extra_avoid': r'diet|zero|sugar free|pink|cloudy'},
    'sparkling_water': {'must': [r'sparkling|carbonated'], 'extra_avoid': r'flavou?r|lemon|lime|berry|wine'},
    'still_water': {'must': [r'still', r'water'], 'extra_avoid': r'flavou?r|lemon|lime|berry|sparkling'},
    'ginger_beer': {'must': [r'ginger beer'], 'allow': ['flavour']},
    'fruit_smoothie': {'must': [r'smoothie']},
    'ready_salted_crisps': {'must': [r'ready salted'], 'allow': ['prepared']},
    'cheese_onion_crisps': {'must': [r'cheese', r'onion'], 'allow': ['prepared', 'flavour']},
    'sea_salt_popcorn': {'must': [r'popcorn'], 'allow': ['prepared'], 'extra_avoid': r'sweet|toffee|butter|caramel'},
    'pretzels': {'must': [r'pretzels?'], 'allow': ['prepared']},
    'rice_cakes': {'must': [r'rice cakes?'], 'allow': ['prepared']},
    'oatcakes': {'must': [r'oatcakes?'], 'allow': ['prepared']},
    'cream_crackers': {'must': [r'cream crackers?'], 'allow': ['prepared']},
    'digestives': {'must': [r'digestives?'], 'allow': ['prepared'], 'extra_avoid': r'chocolate|caramel'},
    'chocolate_digestives': {'must': [r'chocolate', r'digestives?'], 'allow': ['prepared'], 'extra_avoid': r'dark|caramel'},
    'rich_tea': {'must': [r'rich tea'], 'allow': ['prepared']},
    'shortbread': {'must': [r'shortbread'], 'allow': ['prepared']},
    'cookies': {'must': [r'chocolate', r'cookies?'], 'allow': ['prepared']},
    'cereal_bars': {'must': [r'cereal bars?|chewy|granola bars?'], 'allow': ['prepared', 'flavour']},
    'flapjacks': {'must': [r'flapjacks?'], 'allow': ['prepared', 'kids']},
    'milk_chocolate': {'must': [r'milk chocolate'], 'extra_avoid': r'buttons|digestives|biscuits|fingers|chips'},
    'dark_chocolate': {'must': [r'dark chocolate'], 'extra_avoid': r'baking|cooking|chips|drops'},
    'chocolate_buttons': {'must': [r'buttons'], 'allow': ['kids']},
    'jelly_sweets': {'must': [r'jelly|jellies|gums|wine gums|babies|fruit gums'], 'allow': ['prepared', 'kids']},
    'mints': {'must': [r'mints'], 'allow': ['prepared', 'flavour']},
    'dried_mango': {'must': [r'dried', r'mango']},
    'houmous': {'must': [r'houmous|hummus'], 'allow': ['prepared'], 'extra_avoid': r'red pepper|caramelised|chilli|lemon|pine nut|snack'},
    'coleslaw': {'must': [r'coleslaw'], 'allow': ['prepared'], 'extra_avoid': r'cheese|light|reduced'},
    'frozen_pizza_margherita': {'must': [r'margherita|cheese', r'pizza'], 'frozen': True},
    'frozen_pizza_pepperoni': {'must': [r'pepperoni', r'pizza'], 'frozen': True},
    'chicken_nuggets': {'must': [r'chicken', r'nuggets'], 'frozen': True, 'allow': ['kids']},
    'breaded_fish_fillets': {'must': [r'breaded|battered', r'fish|cod|haddock|pollock'], 'frozen': True},
    'frozen_cod': {'must': [r'\bcod\b'], 'frozen': True, 'extra_avoid': r'breaded|battered|fingers|fish cakes?|sauce|bites'},
    'frozen_prawns': {'must': [r'prawns?'], 'frozen': True, 'extra_avoid': r'king|tiger|raw|cocktail|ring|tempura|breaded'},
    'frozen_broccoli': {'must': [r'broccoli'], 'frozen': True},
    'frozen_cauliflower': {'must': [r'cauliflower'], 'frozen': True, 'extra_avoid': r'cheese|rice'},
    'frozen_green_beans': {'must': [r'green beans|fine beans|sliced beans'], 'frozen': True},
    'frozen_mango': {'must': [r'mango'], 'frozen': True, 'extra_avoid': r'smoothie|mix|berries|pineapple'},
    'vanilla_ice_cream': {'must': [r'vanilla', r'ice cream'], 'allow': ['prepared'], 'frozen': True},
    'chocolate_ice_cream': {'must': [r'chocolate', r'ice cream'], 'allow': ['prepared'], 'frozen': True},
    'yorkshire_puddings': {'must': [r'yorkshire'], 'frozen': True},
    'roast_potatoes_frozen': {'must': [r'roast potatoes'], 'frozen': True},
    'potato_waffles': {'must': [r'waffles?'], 'frozen': True, 'extra_avoid': r'sweet|syrup|belgian|chocolate|toaster'},
    'puff_pastry': {'must': [r'puff pastry']},
    'shortcrust_pastry': {'must': [r'shortcrust']},
    'fresh_ravioli': {'must': [r'ravioli'], 'allow': ['flavour']},
    'gnocchi': {'must': [r'gnocchi'], 'extra_avoid': r'sauce|bake|kit|meal'},
    'ready_mac_cheese': {'must': [r'macaroni cheese|mac (and|&|n) cheese|mac n cheese'], 'allow': ['ready', 'prepared']},
    'ready_tikka': {'must': [r'tikka masala', r'rice'], 'allow': ['ready', 'flavour']},
    'fresh_soup': {'must': [r'soup'], 'allow': ['flavour'], 'extra_avoid': r'tin|cup|instant|packet|noodle'},
    'quiche': {'must': [r'quiche'], 'allow': ['prepared'], 'extra_avoid': r'lorraine|ham|bacon|spinach|mini'},
    'spaghetti_hoops': {'must': [r'hoops'], 'allow': ['kids']},
    'toilet_roll': {'must': [r'toilet']},
    'kitchen_roll': {'must': [r'kitchen roll|kitchen towels?']},
    'washing_up_liquid': {'must': [r'washing up|washing-up']},
    'dishwasher_tablets': {'must': [r'dishwasher']},
    'laundry_liquid': {'must': [r'laundry|washing', r'liquid|gel'], 'extra_avoid': r'washing up|washing-up|capsules|pods'},
    'bin_bags': {'must': [r'bin bags?|bin liners?|refuse sacks?|swing bin']},
    'cling_film': {'must': [r'cling']},
    'kitchen_foil': {'must': [r'foil']},
    'baking_paper': {'must': [r'baking paper|parchment']},
    'sponges': {'must': [r'sponges?|scourers?']},
    'multi_surface_spray': {'must': [r'multi|surface', r'spray|cleaner']},
    'bleach': {'must': [r'bleach']},
    'tissues': {'must': [r'tissues'], 'extra_avoid': r'toilet'},
    'shampoo': {'must': [r'shampoo']},
    'conditioner': {'must': [r'conditioner'], 'extra_avoid': r'fabric|shampoo'},
    'shower_gel': {'must': [r'shower']},
    'hand_wash': {'must': [r'hand ?wash'], 'extra_avoid': r'refill'},
    'soap_bars': {'must': [r'soap'], 'extra_avoid': r'liquid|hand ?wash'},
    'toothpaste': {'must': [r'toothpaste']},
    'toothbrushes': {'must': [r'toothbrush'], 'extra_avoid': r'electric|heads'},
    'mouthwash': {'must': [r'mouthwash']},
    'deodorant': {'must': [r'deodorant|anti-?perspirant']},
    'plasters': {'must': [r'plasters']},
    'body_lotion': {'must': [r'body lotion|lotion']},
}
# fixes found by testing the rules against real shop product names (October 2026)
FIX.update({
    'kale': {'must': [r'\bkale\b']},
    'little_gem': {'must': [r'little gem|baby gem']},
    'cos_lettuce': {'must': [r'\bcos\b|romaine']},
    'tomato_soup': {'must': [r'tomato', r'soup']},
    'chicken_soup': {'must': [r'chicken', r'soup']},
    'sundried_tomatoes': {'must': [r'sun[- ]?dried', r'tomato'], 'ok': ['oil']},
    'prunes': {'must': [r'\bprunes?\b']},
    'cardamom': {'must': [r'cardamom']},
    'bay_leaves': {'must': [r'bay lea(f|ves)']},
    'cayenne': {'must': [r'cayenne']},
    'orange_juice': {'must': [r'orange', r'juice'], 'extra_avoid': r'squash|drink|carrot|mango|pineapple|apple|bits|with'},
    'malt_loaf': {'must': [r'malt loaf|\bmalt\b']},
    'anchovies': {'must': [r'anchov'], 'ok': ['oil']},
    'roasted_peppers': {'must': [r'roasted', r'peppers?'], 'ok': ['oil']},
    'tahini': {'must': [r'tahini'], 'ok': ['paste']},
    'guacamole': {'must': [r'guacamole'], 'ok': ['dip']},
    'garlic_granules': {'must': [r'garlic', r'granules|powder'], 'allow': ['flavour'], 'ok': ['powder']},
    'blue_cheese': {'must': [r'stilton|blue cheese|danish blue|gorgonzola|\bblue\b.*\bcheese\b']},
    'ground_coriander': {'must': [r'coriander', r'ground']},
    'ground_ginger': {'must': [r'ginger', r'ground']},
    'lactose_free_milk': {'must': [r'milk', r'lact(ose)?[- ]?free|lacto ?free'], 'allow': ['free_from']},
    'microwave_rice': {'must': [r'basmati', r'microwave|pouch|ready to heat|express|steam'], 'allow': ['ready'],
                       'extra_avoid': r'pilau|egg|mushroom|golden|lemon'},
    'ready_lasagne': {'must': [r'lasagne'], 'allow': ['prepared', 'ready'], 'extra_avoid': r'sheets|pasta|vegetable|veggie|plant'},
    'ricotta': {'must': [r'ricotta'], 'extra_avoid': r'tortel|ravioli|spinach|cannelloni|girasoli'},
    'roast_ham': {'must': [r'\bham\b', r'joint|roast'], 'extra_avoid': r'sliced|slices|wafer|thin|pizza'},
    'skimmed_milk': {'must': [r'skimmed', r'milk'], 'extra_avoid': r'semi|powder'},
    'sweetcorn_cobs': {'must': [r'\bcobs?\b|on the cob'], 'extra_avoid': r'tin|frozen|kernels|niblets|baby|creamed|pop|mini'},
    'tuna_steaks': {'must': [r'tuna', r'steaks?']},
    'turkey_steaks': {'must': [r'turkey', r'steaks?|breast'], 'extra_avoid': r'mince|sliced|slices|roast|joint'},
    'gherkins': {'must': [r'gherkins?'], 'ok': ['vinegar']},
    'beef_meatballs': {'must': [r'beef', r'meatballs?'], 'allow': ['prepared', 'kids']},
    'mixed_beans': {'must': [r'mixed beans|three bean|bean salad'], 'ok': ['salad']},
    'tinned_pineapple': {'must': [r'pineapple'], 'extra_avoid': r'fresh|whole|dried|smoothie|drink'},
    'fresh_tortellini': {'must': [r'tortell[io]ni'], 'allow': ['flavour']},
    'pepperoni': {'must': [r'pepperoni'], 'ok': ['pizza'], 'extra_avoid': r'\bpizzas?\b(?!\s+pepperoni)|stick'},
    'gammon_steaks': {'must': [r'gammon', r'steaks?'], 'allow': ['flavour']},
    'beef_diced': {'must': [r'beef|stewing steak', r'diced|casserole|stewing']},
})
# names on the site that say "frozen": the shop's product must be frozen too (no fresh stand-ins)
MUST_FROZEN = {'frozen_pizza_margherita', 'frozen_pizza_pepperoni', 'frozen_cod', 'frozen_prawns',
               'frozen_broccoli', 'frozen_cauliflower', 'frozen_green_beans', 'frozen_mango', 'roast_potatoes_frozen'}
NONFOOD = {'toilet_roll', 'kitchen_roll', 'washing_up_liquid', 'dishwasher_tablets', 'laundry_liquid', 'bin_bags',
           'cling_film', 'kitchen_foil', 'baking_paper', 'sponges', 'multi_surface_spray', 'bleach', 'tissues',
           'shampoo', 'conditioner', 'shower_gel', 'hand_wash', 'soap_bars', 'toothpaste', 'toothbrushes', 'mouthwash',
           'deodorant', 'plasters', 'body_lotion'}


def words(name):
    t = re.sub(r'\(.*?\)', ' ', name.lower())
    t = t.replace('&', ' and ').replace('’', "'")
    return [w for w in re.findall(r"[a-zà-ÿ]+(?:'[a-z]+)?", t)]


def word_rx(w):
    w = w.replace("'s", '')
    base = re.sub(r'(ies)$', 'y', w)
    if base != w:
        return r'\b' + re.escape(base[:-1]) + r'(y|ies)\b'
    if w.endswith(('us', 'ss', 'is')):
        base = w
    elif w.endswith(('ches', 'shes', 'xes', 'zes', 'sses', 'oes')):
        base = w[:-2]
    elif w.endswith('s') and len(w) > 3:
        base = w[:-1]
    else:
        base = w
    return r'\b' + re.escape(base) + r'(s|es)?\b'


def make_rule(pid, prod, tags):
    name = prod['name']
    low = name.lower()
    fix = FIX.get(pid, {})
    ws = words(name)
    hard = [w for w in ws if w not in SOFT]
    if not hard:
        hard = ws
    must = fix.get('must') or [word_rx(w) for w in hard]
    # the product's own name may belong to an avoid group (a smoked fish, a biscuit): allow that group
    allow = set(fix.get('allow', []))
    for t, rx in tags.items():
        if re.search(rx, low, re.I):
            allow.add(t)
    if pid in NONFOOD:
        allow |= {'flavour', 'kids', 'assorted'}
    other = [w for w in OTHER if w not in low and w not in fix.get('ok', []) and not any(re.search(m, w) for m in must)]
    avoid = r'\b(' + '|'.join(re.escape(w) for w in other) + r')s?\b'
    if fix.get('extra_avoid'):
        avoid = fix['extra_avoid'] + '|' + avoid
    frozen = bool(fix.get('frozen') or 'frozen' in low)
    if pid in MUST_FROZEN:
        must = must + [r'\bfrozen\b']
    kw = re.sub(r'\s+', ' ', re.sub(r'\((.*?)\)', r'\1', name)).strip().lower()
    if frozen and 'frozen' not in kw:
        kw = 'frozen ' + kw
    r = {'kw': kw, 'must': must, 'avoid': avoid, 'auto': True, 'tol': 0.25}
    if allow:
        r['allow'] = sorted(allow)
    if frozen:
        r['frozen'] = True
    if prod['unit'] == 'each':
        r['loose'] = True
    return r


# a product the site counts as ONE container (a pack of toilet rolls, a bottle, a loaf, a pizza): whatever pack the
# shop sells counts as one, so "9 rolls" or "400ml" on the label is not read as the number of units
CONTAINER_WORDS = {'pack', 'kit', 'roll', 'bottle', 'tube', 'can', 'wash', 'loaf', 'pizza', 'baguette'}


def unit_words(html):
    out = {}
    for m in re.finditer(r"^\s{2}([a-z_]+):\{n:'(?:[^'\\]|\\.)*',u:'(\w+)',uw:\[([^\]]*)\]", html, re.M):
        out[m.group(1)] = re.findall(r"'([^']*)'", m.group(3))
    return out


def build(cfg, cat, html=''):
    uw = unit_words(html) if html else {}
    rules = {}
    for pid in cat:
        if pid in cfg['products']:
            continue
        r = make_rule(pid, cat[pid], cfg['tags'])
        words_ = uw.get(pid) or []
        if cat[pid]['unit'] == 'each' and len(cat[pid]['packs']) == 1 and cat[pid]['packs'][0][0] == 1 \
                and words_ and words_[0] in CONTAINER_WORDS:
            r['whole_pack'] = True
        rules[pid] = r
    return rules


def load_with_auto(cfg, path=None):
    """config.json plus the auto rules (hand-written rules always win). Returns a new dict."""
    path = path or os.path.join(HERE, 'auto-rules.json')
    out = dict(cfg)
    out['products'] = dict(cfg['products'])
    if os.path.exists(path):
        auto = json.load(open(path, encoding='utf-8')).get('products', {})
        for pid, r in auto.items():
            out['products'].setdefault(pid, r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--html', default=os.path.join(ROOT, 'index.html'))
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--out', default=os.path.join(HERE, 'auto-rules.json'))
    ap.add_argument('--show', action='store_true')
    a = ap.parse_args()
    import price_sources as PS
    U = PS.load_updater()
    cfg = json.load(open(a.config, encoding='utf-8'))
    html = open(a.html, encoding='utf-8').read()
    cat = U.read_catalogue(html)
    rules = build(cfg, cat, html)
    for pid, r in rules.items():                 # every pattern must compile and must not match empty text
        for rx in r['must'] + [r['avoid']]:
            if re.compile(rx, re.I).search(''):
                sys.exit(f'{pid}: pattern matches empty text: {rx}')
    if a.show:
        for pid, r in rules.items():
            print(f'{pid:24} kw={r["kw"]!r:38} must={r["must"]} allow={r.get("allow", [])}{" FROZEN" if r.get("frozen") else ""}')
        print(len(rules), 'auto rules')
        return
    doc = {'_about': 'Starter rules made by pipeline/auto_rules.py for products with no hand-written rules in config.json. '
                     'Regenerate with "python pipeline/auto_rules.py". To improve a product, move it into config.json '
                     'with hand-written rules (it then drops out of this file).',
           'products': rules}
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f'Wrote {len(rules)} auto rules to {a.out}')


if __name__ == '__main__':
    main()
